import json
import sys
import builtins
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import agents.reporter as reporter_module
import app as flask_app_module
from agents.ai_planner import AISchedulePlanner, PlannedSlot, _build_location_prompt, _enforce_hard_limits, _parse_schedule_response, _score_slots
from agents.optimiser import DATA_DRIVEN_DAILY_RANGES, DAY_ORDER, MAX_TRAINER_WEEKLY_MINUTES, RoomOccupancy, ScheduleOptimiser, ScheduleSlot, TrainerState, get_class_duration, slot_time_to_minutes
from agents.reporter import OutputReporter
from rule_config import build_rules_catalog, load_rules_config
from serve import build_pipeline_command, find_available_port, is_output_artifact_path


def make_slot(**kwargs):
    defaults = dict(
        location="Kwality House, Kemps Corner",
        date="2026-05-04",
        day_of_week="Monday",
        time="09:00",
        class_name="Studio Barre 57",
        trainer_1="Anisha Shah",
        trainer_2="",
        cover="",
        room="studio_a",
        capacity=20,
        duration_min=57,
        predicted_fill_rate=0.50,
        score=75.0,
        recommendation="INCLUDE",
        is_experimental=False,
        scheduling_reason="test",
        historical_avg_fill=0.50,
        historical_avg_checkin=5.0,
        historical_session_count=10,
        constraint_violations=[],
    )
    defaults.update(kwargs)
    return ScheduleSlot(**defaults)


def test_room_selection_falls_back_when_only_same_format_room_is_free():
    rooms = {
        "studio_a": {"capacity": 20, "families": None},
        "studio_b": {"capacity": 13, "families": None},
    }
    occ = RoomOccupancy(rooms)
    occ.occupy("Monday", "studio_a", 540, 57, "Studio Barre 57", "Trainer A")
    occ.occupy("Monday", "studio_b", 600, 57, "Studio Mat 57", "Trainer B")

    # At 10:15, studio_a is free but its previous class has the same family.
    # The scheduler should use it as a fallback instead of failing the slot.
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    room = optimiser._find_best_room(occ, "Monday", "barre_57", 615, 57, "barre_family")

    assert room == "studio_a"


def test_class_mix_max_is_hard_generation_ceiling():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.schedule_config = {
        "class_mix": {
            "Kwality House, Kemps Corner": {
                "Studio HIIT": {"min": 1, "max": 2}
            }
        }
    }

    assert optimiser._weekly_over_target_penalty(
        "Kwality House, Kemps Corner", "Studio HIIT", current_count=1
    ) == 0
    assert optimiser._weekly_over_target_penalty(
        "Kwality House, Kemps Corner", "Studio HIIT", current_count=2
    ) < 0
    assert not optimiser._class_mix_allows_candidate(
        "Kwality House, Kemps Corner", "Studio HIIT", current_count=2
    )
    assert not optimiser._class_mix_hard_blocked(
        "Kwality House, Kemps Corner", "Studio HIIT"
    )


def test_ai_scoring_uses_exact_time_before_day_level_fallback():
    slots = [
        PlannedSlot(
            location="Supreme HQ, Bandra",
            date="2026-05-04",
            day_of_week="Monday",
            time="19:00",
            class_name="Studio Barre 57",
            trainer_1="Anisha Shah",
            trainer_2="",
            cover="",
            room="studio_a",
            capacity=15,
            predicted_fill_rate=0.0,
            score=0.0,
            constraint_violations=[],
        )
    ]
    scores = {
        "class_slot_ranking": [
            {
                "location": "Supreme HQ, Bandra",
                "class": "Studio Barre 57",
                "trainer": "Anisha Shah",
                "day": 0,
                "time": "09:00",
                "score": 20.0,
                "avg_fill_rate": 0.20,
            },
            {
                "location": "Supreme HQ, Bandra",
                "class": "Studio Barre 57",
                "trainer": "Anisha Shah",
                "day": 0,
                "time": "19:00",
                "score": 80.0,
                "avg_fill_rate": 0.80,
            },
        ]
    }

    scored = _score_slots(slots, scores)

    assert scored[0].score == 80.0
    assert scored[0].predicted_fill_rate == 0.80


def test_ai_scoring_uses_class_slot_group_when_trainer_history_missing():
    slots = [
        PlannedSlot(
            location="Supreme HQ, Bandra",
            date="2026-05-04",
            day_of_week="Monday",
            time="19:00",
            class_name="Studio Barre 57",
            trainer_1="New Trainer",
            trainer_2="",
            cover="",
            room="studio_a",
            capacity=15,
            predicted_fill_rate=0.0,
            score=0.0,
            constraint_violations=[],
        )
    ]
    scores = {
        "slot_group_ranking": [
            {
                "location": "Supreme HQ, Bandra",
                "class": "Studio Barre 57",
                "day": 0,
                "time": "19:00",
                "score": 86.0,
                "avg_fill_rate": 0.91,
            }
        ],
        "class_slot_ranking": [
            {
                "location": "Supreme HQ, Bandra",
                "class": "Studio Barre 57",
                "trainer": "Other Trainer",
                "day": 0,
                "time": "19:00",
                "score": 64.0,
                "avg_fill_rate": 0.61,
            }
        ],
    }

    scored = _score_slots(slots, scores)

    assert scored[0].score == 86.0
    assert scored[0].predicted_fill_rate == 0.91


def test_ai_parser_skips_disabled_trainers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    Path("config/schedule_config.json").write_text(json.dumps({"inactive_trainers": ["Nishanth Raj"]}))
    raw = json.dumps({
        "schedule": [
            {"day": "Monday", "time": "09:00", "class": "Studio Barre 57", "trainer": "Nishanth Raj"},
            {"day": "Monday", "time": "10:00", "class": "Studio Barre 57", "trainer": "Active Trainer"},
        ]
    })

    slots, errors = _parse_schedule_response(raw, "Kwality House, Kemps Corner", "2026-05-04", {
        "Active Trainer": {"name": "Active Trainer", "active": True}
    })

    assert [s.trainer_1 for s in slots] == ["Active Trainer"]
    assert any("Disabled trainer skipped: Nishanth Raj" in e for e in errors)


def test_ai_hard_limit_enforcer_drops_profile_disabled_trainers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    Path("config/schedule_config.json").write_text(json.dumps({}))
    slots = [
        make_slot(trainer_1="Disabled Trainer"),
        make_slot(time="10:00", trainer_1="Active Trainer"),
    ]

    kept = _enforce_hard_limits(slots, "Kwality House, Kemps Corner", {
        "Disabled Trainer": {"name": "Disabled Trainer", "active": False},
        "Active Trainer": {
            "name": "Active Trainer",
            "active": True,
            "locations": {"Kwality House, Kemps Corner": {"available_days": ["Monday"]}},
        },
    })

    assert [s.trainer_1 for s in kept] == ["Active Trainer"]


def test_scorecard_reports_historical_evidence_exposure():
    reporter = OutputReporter()
    slots = [
        make_slot(historical_session_count=10, historical_avg_fill=0.60, predicted_fill_rate=0.60).__dict__,
        make_slot(historical_session_count=0, historical_avg_fill=0.0, predicted_fill_rate=0.20).__dict__,
    ]

    entry = reporter._build_scorecard_entry("Kwality House, Kemps Corner", slots)

    assert entry["zero_history_slots"] == 1
    assert entry["zero_history_pct"] == 0.5
    assert entry["historical_avg_fill_rate"] == 0.3


def test_scorecard_class_mix_canonicalizes_class_variants():
    reporter = OutputReporter()
    slots = [
        make_slot(class_name="Studio Strength Lab (Push)").__dict__,
        make_slot(class_name="Studio Strength Lab (Full Body)").__dict__,
        make_slot(class_name="Studio Back Body Blaze Express").__dict__,
        make_slot(class_name="Studio Mat 57 Express").__dict__,
    ]

    entry = reporter._build_scorecard_entry("Kwality House, Kemps Corner", slots)

    assert entry["class_mix"] == {
        "Studio Strength Lab": 0.5,
        "Studio Back Body Blaze": 0.25,
        "Studio Mat 57": 0.25,
    }
    assert entry["format_counts"]["Studio Strength Lab"] == 2
    assert entry["format_counts"]["Studio Back Body Blaze"] == 1
    assert entry["format_counts"]["Studio Mat 57"] == 1


def test_scorecard_schedule_score_uses_relative_baseline_and_floor_target():
    reporter = OutputReporter()
    slots = [
        make_slot(score=80.0).__dict__,
        make_slot(score=70.0).__dict__,
    ]

    entry = reporter._build_scorecard_entry(
        "Kwality House, Kemps Corner",
        slots,
        score_baselines={"Kwality House, Kemps Corner": 75.0},
    )

    assert entry["target_schedule_score"] == 80
    assert entry["schedule_score"] == 100.0


def test_scorecard_schedule_score_penalizes_underperforming_schedule():
    reporter = OutputReporter()
    slots = [
        make_slot(score=30.0).__dict__,
        make_slot(score=20.0).__dict__,
    ]

    entry = reporter._build_scorecard_entry(
        "Kwality House, Kemps Corner",
        slots,
        score_baselines={"Kwality House, Kemps Corner": 80.0},
    )

    assert entry["schedule_score"] < 80


def test_reporter_validates_daily_target_range(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "schedule_config.json").write_text(json.dumps({
        "targets": {
            "Kwality House, Kemps Corner": {
                "Monday": {"target": 2, "max": 4},
            }
        }
    }))
    reporter = OutputReporter()
    slots = [
        make_slot(time="09:00").__dict__,
        make_slot(time="10:00").__dict__,
        make_slot(time="11:00").__dict__,
    ]

    errors, warnings = reporter._validate_against_schedule_config({
        "Kwality House, Kemps Corner": slots,
    })

    assert not errors

    errors, warnings = reporter._validate_against_schedule_config({
        "Kwality House, Kemps Corner": slots[:1],
    })

    assert any("below min 2" in e for e in errors)


def test_reporter_warns_on_class_mix_floor_mismatch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "schedule_config.json").write_text(json.dumps({
        "class_mix": {
            "Kwality House, Kemps Corner": {
                "Studio Mat 57": {"min": 1, "max": 2},
            }
        }
    }))
    reporter = OutputReporter()
    slots = [make_slot(class_name="Studio Barre 57").__dict__]

    errors, warnings = reporter._validate_against_schedule_config({
        "Kwality House, Kemps Corner": slots,
    })

    assert not errors
    assert any("Studio Mat 57 count 0 < 1" in w for w in warnings)


def test_schedule_config_api_persists_source_of_truth_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(flask_app_module, "PROJECT_ROOT", tmp_path)
    (tmp_path / "config").mkdir()
    payload = {
        "targets": {
            "Supreme HQ, Bandra": {
                "Saturday": {"target": 13, "max": 13, "source": "settings"}
            }
        },
        "class_mix": {
            "Supreme HQ, Bandra": {
                "Studio PowerCycle": {"min": 21, "max": 24, "source": "settings"}
            }
        },
        "manual_protected": [],
        "manual_excluded": [],
        "custom_rules": [
            {
                "enabled": True,
                "priority": "hard",
                "rule_type": "daily_target",
                "location": "Supreme HQ, Bandra",
                "day": "Saturday",
                "operator": "exactly",
                "value": 13,
                "superseded_by": "settings.targets",
            }
        ],
        "source_of_truth": {
            "owner": "settings-command-center",
            "conflict_policy": "settings_override_soft_rules",
        },
    }

    response = flask_app_module.app.test_client().post(
        "/api/save-schedule-config",
        json=payload,
    )

    assert response.status_code == 200
    assert json.loads((tmp_path / "config" / "schedule_config.json").read_text()) == payload


def test_schedule_data_json_is_not_routed_as_output_artifact():
    assert not is_output_artifact_path("/schedule_data.json")
    assert not is_output_artifact_path("/schedule_data_2026-05-04.json")
    assert is_output_artifact_path("/schedule_kwality.xlsx")
    assert is_output_artifact_path("/schedule_supreme_detailed.csv")
    assert is_output_artifact_path("/ai_insights.json")
    assert is_output_artifact_path("/scorecard.json")


def test_rules_catalog_includes_command_center_metadata():
    catalog = build_rules_catalog(load_rules_config())
    all_rules = [rule for group in catalog["groups"] for rule in group["rules"]]

    assert all_rules
    assert all(rule.get("impact_area") for rule in all_rules)
    assert all(rule.get("risk_level") in {"critical", "high", "medium", "low"} for rule in all_rules)
    assert all(rule.get("status_tag") in {"Recommended", "Risky", "Disabled"} for rule in all_rules)

    format_rules = [rule for rule in all_rules if rule.get("type") == "class_format"]
    assert format_rules
    assert all(rule["impact_area"] == "Class format policy" for rule in format_rules)


def test_find_available_port_skips_occupied_port():
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("", 0))
    occupied = sock.getsockname()[1]
    sock.listen(1)
    try:
        assert find_available_port(occupied, host="") != occupied
    finally:
        sock.close()


def test_default_daily_ranges_match_requested_weekly_totals():
    expected = {
        "Kwality House, Kemps Corner": (70, 80),
        "Supreme HQ, Bandra": (60, 70),
        "Kenkere House": (50, 60),
    }

    for location, (weekly_min, weekly_max) in expected.items():
        ranges = DATA_DRIVEN_DAILY_RANGES[location]
        assert sum(ranges[day][0] for day in DAY_ORDER) == weekly_min
        assert sum(ranges[day][1] for day in DAY_ORDER) == weekly_max


def test_trainer_cannot_work_am_and_pm_on_same_day():
    state = TrainerState("Anisha Shah", 1)

    assert state.can_add("Monday", "09:00", "Kwality House, Kemps Corner", "Studio Barre 57", 4, "07:00", "20:30")
    state.add("Monday", "09:00", "Kwality House, Kemps Corner", "Studio Barre 57")

    assert state.can_add("Monday", "11:00", "Kwality House, Kemps Corner", "Studio Mat 57", 4, "07:00", "20:30")
    assert not state.can_add("Monday", "18:00", "Kwality House, Kemps Corner", "Studio Barre 57", 4, "07:00", "20:30")


def test_trainer_cannot_work_multiple_main_studios_in_same_shift():
    state = TrainerState("Trainer A", 1)

    assert state.can_add("Monday", "07:00", "Kwality House, Kemps Corner", "Studio Barre 57", 4, "07:00", "20:30")
    state.add("Monday", "07:00", "Kwality House, Kemps Corner", "Studio Barre 57")

    assert state.can_add("Monday", "10:00", "Kwality House, Kemps Corner", "Studio Mat 57", 4, "07:00", "20:30")
    assert not state.can_add("Monday", "10:00", "Supreme HQ, Bandra", "Studio Barre 57", 4, "07:00", "20:30")
    assert not state.can_add("Monday", "10:00", "Kenkere House", "Studio Barre 57", 4, "07:00", "20:30")


def test_courtside_can_only_be_final_same_city_shift_stop():
    state = TrainerState("Trainer A", 1)

    state.add("Saturday", "07:00", "Kwality House, Kemps Corner", "Studio Barre 57")

    assert state.can_add("Saturday", "10:00", "Courtside", "Studio Mat 57", 4, "07:00", "20:30")
    state.add("Saturday", "10:00", "Courtside", "Studio Mat 57")

    assert not state.can_add("Saturday", "11:00", "Kwality House, Kemps Corner", "Studio Barre 57", 4, "07:00", "20:30")
    assert not state.can_add("Saturday", "11:00", "Courtside", "Studio FIT", 4, "07:00", "20:30")


def test_courtside_is_blocked_when_trainer_has_later_main_studio_class():
    state = TrainerState("Trainer A", 1)

    state.add("Saturday", "11:00", "Supreme HQ, Bandra", "Studio Barre 57")

    assert not state.can_add("Saturday", "10:00", "Courtside", "Studio Mat 57", 4, "07:00", "20:30")


def test_trainer_cannot_exceed_weekly_15_hour_cap():
    state = TrainerState("Anisha Shah", 1)
    state.weekly_minutes = MAX_TRAINER_WEEKLY_MINUTES - get_class_duration("Studio Barre 57") + 1

    assert not state.can_add("Monday", "09:00", "Kwality House, Kemps Corner", "Studio Barre 57", 4, "07:00", "20:30")


def test_lower_tier_is_blocked_when_eligible_tier1_is_under_target():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.trainer_profiles = {
        "Tier One": {
            "locations": {
                "Kwality House, Kemps Corner": {
                    "available_days": ["Monday"],
                    "time_window": {"start": "07:00", "end": "20:30"},
                    "max_classes_per_day": 4,
                }
            },
            "qualifications": {"all_barre": True},
        },
        "Tier Two": {
            "locations": {
                "Kwality House, Kemps Corner": {
                    "available_days": ["Monday"],
                    "time_window": {"start": "07:00", "end": "20:30"},
                    "max_classes_per_day": 4,
                }
            },
            "qualifications": {"all_barre": True},
        },
    }
    optimiser.trainer_states = {
        "Tier One": TrainerState("Tier One", 1),
        "Tier Two": TrainerState("Tier Two", 2),
    }

    assert optimiser._tier1_under_target_exists_for_slot(
        "Kwality House, Kemps Corner",
        "Monday",
        "2026-05-04",
        "09:00",
        "Studio Barre 57",
        set(),
        exclude_trainer="Tier Two",
    )


def test_tier1_priority_score_dominates_lower_tier_history_edge():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.trainer_states = {
        "Tier One": TrainerState("Tier One", 1),
        "Tier Two": TrainerState("Tier Two", 2),
    }
    optimiser.trainer_states["Tier Two"].weekly_minutes = 10 * 60

    assert optimiser._tier_priority_score("Tier One") > optimiser._tier_priority_score("Tier Two") + 200


def test_schedule_config_targets_are_selected_within_min_max_range(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "schedule_config.json").write_text(
        '{"targets":{"Kwality House, Kemps Corner":{"Monday":{"target":10,"max":14}}}}'
    )

    picked = {
        ScheduleOptimiser(target_week_start="2026-05-04", locations=[], variation_seed=seed)._pick_daily_target(
            "Kwality House, Kemps Corner", "Monday"
        )
        for seed in [0, 1, 42, 137, 999]
    }

    assert all(10 <= value <= 14 for value in picked)
    assert len(picked) > 1


def test_optimiser_daily_top_up_uses_selected_target_within_range(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "schedule_config.json").write_text(json.dumps({
        "targets": {
            "Supreme HQ, Bandra": {
                "Monday": {"target": 2, "max": 4}
            }
        }
    }))

    optimiser = ScheduleOptimiser(
        target_week_start="2026-05-04",
        locations=["Supreme HQ, Bandra"],
    )
    optimiser.schedule_config = json.loads((tmp_path / "config" / "schedule_config.json").read_text())
    optimiser.trainer_states = {"Trainer B": TrainerState("Trainer B", 2)}
    monkeypatch.setattr(optimiser, "_pick_daily_target", lambda location, day_name: 3)

    all_slots = [
        make_slot(location="Supreme HQ, Bandra", day_of_week="Monday", time="09:00", trainer_1="Trainer A", room="studio_a"),
        make_slot(location="Supreme HQ, Bandra", day_of_week="Monday", time="10:00", trainer_1="Trainer C", room="studio_b"),
    ]
    room_occ = RoomOccupancy({
        "powercycle": {"capacity": 14, "families": ["powercycle"]},
        "studio_a": {"capacity": 14, "families": None},
        "studio_b": {"capacity": 14, "families": None},
    })

    def fake_fill_slot(location, day_name, date_str, time_str, used_at_time, shift_trainers,
                       room_occ, slots_today, exp_today, opt_today, is_prime=False,
                       weekly_class_counts=None, class_format_count_today=None):
        if time_str == "10:15":
            return make_slot(
                location=location,
                date=date_str,
                day_of_week=day_name,
                time=time_str,
                class_name="Studio Mat 57",
                trainer_1="Trainer B",
                room="studio_b",
                capacity=14,
            )
        return None

    monkeypatch.setattr(optimiser, "_fill_slot", fake_fill_slot)

    optimiser._daily_target_top_up(
        "Supreme HQ, Bandra",
        date.fromisoformat("2026-05-04"),
        all_slots,
        room_occ,
        weekly_class_counts={},
        am_slots=["10:15"],
        pm_slots=[],
    )

    assert len([s for s in all_slots if s.day_of_week == "Monday"]) == 3


def test_optimiser_daily_target_top_up_repairs_underfilled_day(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "schedule_config.json").write_text(json.dumps({
        "targets": {
            "Supreme HQ, Bandra": {
                "Monday": {"target": 2, "max": 2}
            }
        }
    }))

    optimiser = ScheduleOptimiser(
        target_week_start="2026-05-04",
        locations=["Supreme HQ, Bandra"],
    )
    optimiser.schedule_config = json.loads((tmp_path / "config" / "schedule_config.json").read_text())
    optimiser.trainer_states = {"Trainer B": TrainerState("Trainer B", 2)}

    existing = make_slot(
        location="Supreme HQ, Bandra",
        day_of_week="Monday",
        time="09:00",
        class_name="Studio Barre 57",
        trainer_1="Trainer A",
        room="studio_a",
        capacity=14,
    )
    all_slots = [existing]
    room_occ = RoomOccupancy({
        "powercycle": {"capacity": 14, "families": ["powercycle"]},
        "studio_a": {"capacity": 14, "families": None},
        "studio_b": {"capacity": 14, "families": None},
    })
    room_occ.occupy("Monday", "studio_a", slot_time_to_minutes("09:00"), 57, "Studio Barre 57", "Trainer A")

    def fake_fill_slot(location, day_name, date_str, time_str, used_at_time, shift_trainers,
                       room_occ, slots_today, exp_today, opt_today, is_prime=False,
                       weekly_class_counts=None, class_format_count_today=None):
        if time_str == "10:15":
            return make_slot(
                location=location,
                date=date_str,
                day_of_week=day_name,
                time=time_str,
                class_name="Studio Mat 57",
                trainer_1="Trainer B",
                room="studio_b",
                capacity=14,
            )
        return None

    monkeypatch.setattr(optimiser, "_fill_slot", fake_fill_slot)

    optimiser._daily_target_top_up(
        "Supreme HQ, Bandra",
        date.fromisoformat("2026-05-04"),
        all_slots,
        room_occ,
        weekly_class_counts={},
        am_slots=["10:15"],
        pm_slots=[],
    )

    assert len([s for s in all_slots if s.day_of_week == "Monday"]) == 2


def test_optimiser_loads_manual_protected_pins_from_schedule_config():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.schedule_config = {
        "manual_protected": [
            {
                "id": "manual-pin-1",
                "location": "Supreme HQ, Bandra",
                "day": "Monday",
                "time": "07:30",
                "class": "Studio Barre 57",
                "trainer": "Anisha Shah",
                "room": "studio_a",
                "note": "Founder request",
            },
            {
                "location": "Kenkere House",
                "day_of_week": "Monday",
                "time": "09:00",
                "class_name": "Studio Mat 57",
                "trainer_1": "Pushyank Nahar",
            },
        ]
    }

    pins = optimiser._get_pinned_slots("Supreme HQ, Bandra", "Monday")

    assert pins[0] == {
        "id": "manual-pin-1",
        "time": "07:30",
        "trainer": "Anisha Shah",
        "class": "Studio Barre 57",
        "room": "studio_a",
        "note": "Founder request",
        "manual": True,
    }


def test_optimiser_applies_custom_rule_targets_and_restrictions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "schedule_config.json").write_text(json.dumps({
        "custom_rules": [
            {
                "enabled": True,
                "priority": "hard",
                "rule_type": "daily_target",
                "location": "Supreme HQ, Bandra",
                "day": "Monday",
                "operator": "exactly",
                "value": 9,
            },
            {
                "enabled": True,
                "priority": "hard",
                "rule_type": "class_time_restriction",
                "location": "Supreme HQ, Bandra",
                "day": "Monday",
                "time": "10:15",
                "class_name": "Studio Mat 57",
                "operator": "never",
            },
        ]
    }))

    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])

    assert optimiser._pick_daily_target("Supreme HQ, Bandra", "Monday") == 9
    assert optimiser._custom_rule_blocks(
        "Supreme HQ, Bandra", "Monday", "10:15", "Studio Mat 57", "Any Trainer"
    )
    assert not optimiser._custom_rule_blocks(
        "Supreme HQ, Bandra", "Monday", "10:15", "Studio Barre 57", "Any Trainer"
    )


def test_ai_location_prompt_uses_persisted_schedule_targets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "schedule_config.json").write_text(
        json.dumps({
            "targets": {
                "Kwality House, Kemps Corner": {
                    "Monday": {"target": 13, "max": 14},
                    "Tuesday": {"target": 12, "max": 14},
                    "Wednesday": {"target": 12, "max": 14},
                    "Thursday": {"target": 12, "max": 14},
                    "Friday": {"target": 11, "max": 12},
                    "Saturday": {"target": 14, "max": 14},
                    "Sunday": {"target": 4, "max": 6},
                }
            }
        })
    )

    prompt = _build_location_prompt(
        "Kwality House, Kemps Corner",
        "2026-05-04",
        {"class_slot_ranking": []},
        {"trainer_metrics": [], "day_band_metrics": []},
        profiles=[],
    )

    assert "  Monday: 13" in prompt
    assert "  Sunday: 4" in prompt
    assert "  WEEK TOTAL: 78" in prompt


def test_pipeline_command_includes_variation_and_output_suffix():
    cmd = build_pipeline_command(
        "Sessions Performance Data.csv",
        "2026-05-04",
        variation_seed=12345,
        output_suffix="run_test",
    )

    assert "--variation-seed" in cmd
    assert "12345" in cmd
    assert "--output-suffix" in cmd
    assert "run_test" in cmd


def test_optimiser_candidate_rows_are_indexed_by_location_and_day():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    row_kw_mon = {"location": "Kwality House, Kemps Corner", "day": 0, "class": "Studio Barre 57"}
    row_kw_tue = {"location": "Kwality House, Kemps Corner", "day": 1, "class": "Studio Mat 57"}
    row_sup_mon = {"location": "Supreme HQ, Bandra", "day": 0, "class": "Studio FIT"}
    optimiser.scores_data = {"class_slot_ranking": [row_kw_mon, row_kw_tue, row_sup_mon]}

    optimiser._build_score_indexes()

    assert optimiser._candidate_rows("Kwality House, Kemps Corner", 0, day_filter=True) == [row_kw_mon]
    assert optimiser._candidate_rows("Kwality House, Kemps Corner", 0, day_filter=False) == [
        row_kw_mon,
        row_kw_tue,
    ]


def test_optimiser_history_slot_lookup_uses_precomputed_nearby_aggregate():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.hist_lookup = {
        ("Kwality House, Kemps Corner", "Studio Barre 57", "Trainer A", 0, "09:00"): {
            "session_count": 2,
            "avg_fill_rate": 0.50,
            "avg_checkin": 8.0,
            "avg_late_cancel_rate": 0.10,
            "avg_no_show_rate": 0.20,
        },
        ("Kwality House, Kemps Corner", "Studio Barre 57", "Trainer B", 0, "09:15"): {
            "session_count": 3,
            "avg_fill_rate": 0.80,
            "avg_checkin": 11.0,
            "avg_late_cancel_rate": 0.00,
            "avg_no_show_rate": 0.10,
        },
        ("Supreme HQ, Bandra", "Studio Barre 57", "Trainer A", 0, "09:00"): {
            "session_count": 10,
            "avg_fill_rate": 0.10,
            "avg_checkin": 1.0,
        },
    }

    optimiser._build_history_indexes()
    hist = optimiser._get_hist_slot("Kwality House, Kemps Corner", "Studio Barre 57", 0, "09:10")

    assert hist["session_count"] == 5
    assert hist["avg_fill_rate"] == pytest.approx(0.68)
    assert hist["avg_checkin"] == pytest.approx(9.8)


def test_pipeline_state_marks_missing_child_process_as_failed(monkeypatch):
    state = {
        "running": True,
        "status": "running",
        "pid": 12345,
        "started": 1.0,
        "message": "Running",
    }
    monkeypatch.setattr(flask_app_module, "_pipeline_process_alive", lambda pid: False)

    flask_app_module._refresh_pipeline_state(state)

    assert state["running"] is False
    assert state["status"] == "failed"
    assert state["pid"] is None
    assert "stopped" in state["message"].lower()


def test_ai_fallback_generates_three_named_optimisation_iterations(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    class FakeScheduleOptimiser:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            calls.append(kwargs)

        def run(self):
            mode = self.kwargs["optimization_mode"]
            return {
                "target_week_start": self.kwargs["target_week_start"],
                "schedule": [
                    {
                        "location": "Kenkere House",
                        "date": self.kwargs["target_week_start"],
                        "day_of_week": "Monday",
                        "time": "09:00",
                        "class_name": f"Studio Barre 57 {mode}",
                        "trainer_1": "Trainer A",
                        "score": 90.0,
                        "constraint_violations": [],
                    }
                ],
                "optimization_mode": mode,
            }

    monkeypatch.setattr("agents.optimiser.ScheduleOptimiser", FakeScheduleOptimiser)

    planner = AISchedulePlanner(
        target_week_start="2026-05-04",
        locations=["Kenkere House"],
        variation_seed=100,
        output_suffix="web_run",
    )
    output = planner._fallback()

    assert [c["optimization_mode"] for c in calls] == [
        "max_score",
        "trainer_hours",
        "class_variety",
    ]
    assert [i["iteration_name"] for i in output["iterations"]] == [
        "Max Score",
        "Trainer Hours",
        "Class Variety",
    ]
    assert output["schedule"] == output["iterations"][0]["schedule"]
    assert output["iteration_names"] == ["Max Score", "Trainer Hours", "Class Variety"]


def test_ai_fallback_with_output_suffix_refreshes_canonical_draft(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class FakeScheduleOptimiser:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self):
            mode = self.kwargs["optimization_mode"]
            return {
                "target_week_start": self.kwargs["target_week_start"],
                "schedule": [
                    {
                        "location": "Kenkere House",
                        "date": self.kwargs["target_week_start"],
                        "day_of_week": "Monday",
                        "time": "09:00",
                        "class_name": f"Studio Barre 57 {mode}",
                        "trainer_1": "Trainer A",
                        "score": 90.0,
                        "constraint_violations": [],
                    }
                ],
                "optimization_mode": mode,
            }

    monkeypatch.setattr("agents.optimiser.ScheduleOptimiser", FakeScheduleOptimiser)

    planner = AISchedulePlanner(
        target_week_start="2026-05-04",
        locations=["Kenkere House"],
        variation_seed=100,
        output_suffix="web_run",
    )
    output = planner._fallback()

    suffixed_path = tmp_path / "state" / "05_draft_schedule_web_run.json"
    canonical_path = tmp_path / "state" / "05_draft_schedule.json"

    assert suffixed_path.exists()
    assert canonical_path.exists()
    assert json.loads(suffixed_path.read_text())["schedule"] == output["schedule"]
    assert json.loads(canonical_path.read_text())["schedule"] == output["schedule"]


def test_ai_fallback_promotes_daily_target_valid_iteration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "schedule_config.json").write_text(json.dumps({
        "targets": {
            "Supreme HQ, Bandra": {
                "Saturday": {"target": 2, "max": 2}
            }
        }
    }))

    class FakeScheduleOptimiser:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self):
            mode = self.kwargs["optimization_mode"]
            slots = [
                {
                    "location": "Supreme HQ, Bandra",
                    "date": self.kwargs["target_week_start"],
                    "day_of_week": "Saturday",
                    "time": "09:00",
                    "class_name": f"Studio Barre 57 {mode}",
                    "trainer_1": "Trainer A",
                    "score": 90.0,
                    "constraint_violations": [],
                }
            ]
            if mode == "trainer_hours":
                slots.append({
                    "location": "Supreme HQ, Bandra",
                    "date": self.kwargs["target_week_start"],
                    "day_of_week": "Saturday",
                    "time": "10:00",
                    "class_name": "Studio Mat 57",
                    "trainer_1": "Trainer B",
                    "score": 80.0,
                    "constraint_violations": [],
                })
            return {
                "target_week_start": self.kwargs["target_week_start"],
                "schedule": slots,
                "optimization_mode": mode,
            }

    monkeypatch.setattr("agents.optimiser.ScheduleOptimiser", FakeScheduleOptimiser)

    planner = AISchedulePlanner(
        target_week_start="2026-05-04",
        locations=["Supreme HQ, Bandra"],
        output_suffix="web_run",
    )
    output = planner._fallback()

    assert len(output["schedule"]) == 2
    assert output["selected_iteration_name"] == "Trainer Hours"
    assert output["schedule"] == output["iterations"][1]["schedule"]


def test_reporter_iteration_names_prefer_schedule_metadata():
    reporter = OutputReporter()
    schedules = [
        {"iteration_name": "Max Score"},
        {"iteration_name": "Trainer Hours"},
        {"iteration_name": "Class Variety"},
    ]

    assert reporter._iteration_names_for(schedules) == [
        "Max Score",
        "Trainer Hours",
        "Class Variety",
    ]


def test_reporter_run_uses_selected_primary_schedule_for_assertions(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "outputs"
    web_dir = tmp_path / "web"
    state_dir.mkdir()
    output_dir.mkdir()
    web_dir.mkdir()
    (state_dir / "02_metrics.json").write_text("{}")

    monkeypatch.setattr(reporter_module, "STATE_DIR", state_dir)
    monkeypatch.setattr(reporter_module, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(reporter_module, "WEB_DIR", web_dir)
    monkeypatch.setattr(OutputReporter, "_write_csv", lambda *args, **kwargs: None)
    monkeypatch.setattr(OutputReporter, "_write_detailed_csv", lambda *args, **kwargs: None)
    monkeypatch.setattr(OutputReporter, "_write_excel_multi_sheet", lambda *args, **kwargs: None)
    monkeypatch.setattr(OutputReporter, "_write_schedule_data", lambda *args, **kwargs: None)
    monkeypatch.setattr(OutputReporter, "_generate_web_interface", lambda *args, **kwargs: None)
    monkeypatch.setattr(OutputReporter, "_print_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(OutputReporter, "_run_iteration_score_assertions", lambda *args, **kwargs: None)

    bad_iteration = {
        "target_week_start": "2026-05-04",
        "schedule": [make_slot(location="Supreme HQ, Bandra", day_of_week="Monday").__dict__],
        "iteration_name": "Max Score",
    }
    selected_schedule = [
        make_slot(location="Supreme HQ, Bandra", day_of_week="Sunday").__dict__,
        make_slot(location="Kenkere House", day_of_week="Wednesday").__dict__,
    ]
    composite_draft = {
        "target_week_start": "2026-05-04",
        "schedule": selected_schedule,
        "iterations": [bad_iteration],
        "selected_iteration_name": "Trainer Hours",
    }

    seen = {}

    def capture_assertions(self, scorecard, primary_by_location):
        seen["primary_by_location"] = primary_by_location

    monkeypatch.setattr(OutputReporter, "_run_assertions", capture_assertions)

    OutputReporter().run(
        all_schedules=composite_draft["iterations"],
        primary_draft=composite_draft,
    )

    assert [s["day_of_week"] for s in seen["primary_by_location"]["Supreme HQ, Bandra"]] == ["Sunday"]
    assert [s["day_of_week"] for s in seen["primary_by_location"]["Kenkere House"]] == ["Wednesday"]


def test_reporter_web_interface_uses_cached_schedule_json_when_file_read_fails(tmp_path, monkeypatch):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "template.html").write_text(
        "const SCHEDULE_DATA = /*INJECT_SCHEDULE_DATA*/;\n"
        "const SCORECARD = /*INJECT_SCORECARD*/;\n"
        "const WEEK_LABEL = /*INJECT_WEEK_LABEL*/;\n"
        "const OPPORTUNITIES = /*INJECT_OPPORTUNITIES*/;\n"
        "</body>",
        encoding="utf-8",
    )

    monkeypatch.setattr(reporter_module, "WEB_DIR", web_dir)
    reporter = OutputReporter()
    reporter._last_schedule_json = '{"locations": {}}'

    real_open = builtins.open

    def flaky_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if Path(path).name == "schedule_data.json" and "r" in mode:
            raise OSError(5, "Input/output error")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)

    reporter._generate_web_interface({}, "2026-05-04", {}, {}, None)

    html = (web_dir / "index.html").read_text(encoding="utf-8")
    assert 'const SCHEDULE_DATA = {"locations": {}};' in html
