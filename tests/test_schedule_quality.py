import json
import sys
import builtins
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import agents.reporter as reporter_module
import app as flask_app_module
import serve as serve_module
from agents.scorer import _is_top_performer_protected
from agents.ai_planner import AISchedulePlanner, PlannedSlot, _build_location_prompt, _build_system_prompt, _daily_target_errors, _enforce_hard_limits, _parse_schedule_response, _score_slots, _select_primary_iteration, _validate_slots
from agents.optimiser import DATA_DRIVEN_DAILY_RANGES, DAY_ORDER, LOCATION_WEEKLY_CLASS_BOUNDS, MAX_TRAINER_WEEKLY_MINUTES, RoomOccupancy, ScheduleOptimiser, ScheduleSlot, TIER1_WEEKLY_TARGET_MIN, TrainerState, canonical_class_key, class_difficulty_level, get_class_duration, has_early_repair_slot_evidence, is_low_performing_history, is_protected_strength_lab_row, same_protected_class_variant, slot_is_in_blocked_window, slot_time_to_minutes
from agents.reporter import OutputReporter
from rule_config import build_rules_catalog, load_rules_config
from serve import build_pipeline_command, find_available_port, is_output_artifact_path


def web_ui_source() -> str:
    web_dir = Path("web")
    return "\n".join(
        (web_dir / name).read_text()
        for name in ("template.html", "app.css", "app.js")
        if (web_dir / name).exists()
    )


def clear_ai_env(monkeypatch):
    for key in ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "")


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


def test_custom_rule_trainer_availability_max_applies_weekly_cap():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.schedule_config = {
        "custom_rules": [
            {
                "enabled": True,
                "priority": "hard",
                "rule_type": "trainer_availability",
                "operator": "max",
                "trainer": "Karan Bhatia",
                "location": "",
                "value": 7,
            }
        ]
    }
    optimiser.overrides = {}

    assert optimiser._max_per_week("Karan Bhatia", "Kwality House, Kemps Corner") == 7
    assert optimiser._max_per_week("Other Trainer", "Kwality House, Kemps Corner") is None


def test_copper_class_mix_uses_copper_specific_limits_before_canonical_group():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.schedule_config = {
        "class_mix": {
            "Copper & Cloves": {
                "Studio Mat 57": {"min": 0, "max": 0},
                "Copper + Cloves Mat 57": {"min": 1, "max": 6},
            }
        }
    }

    assert canonical_class_key("Copper + Cloves Mat 57") == "Copper + Cloves Mat 57"
    assert not optimiser._class_mix_hard_blocked("Copper & Cloves", "Copper + Cloves Mat 57")
    assert optimiser._class_mix_allows_candidate("Copper & Cloves", "Copper + Cloves Mat 57", current_count=5)
    assert not optimiser._class_mix_allows_candidate("Copper & Cloves", "Copper + Cloves Mat 57", current_count=6)


def test_supreme_eight_to_nine_window_prioritizes_sub_hour_slots():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.slot_availability = {
        "Supreme HQ, Bandra": [
            {"time": "08:00", "viable": True},
            {"time": "08:30", "viable": True},
            {"time": "08:45", "viable": True},
            {"time": "09:00", "viable": True},
            {"time": "09:30", "viable": True},
            {"time": "18:00", "viable": True},
        ]
    }

    am_slots, _ = optimiser._get_viable_slots("Supreme HQ, Bandra")

    assert am_slots.index("08:30") < am_slots.index("09:00")
    assert am_slots.index("08:45") < am_slots.index("09:00")
    assert "11:15" in am_slots
    assert "11:45" in am_slots


def test_supreme_eight_to_nine_window_adds_missing_0830_bridge_slot():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.slot_availability = {
        "Supreme HQ, Bandra": [
            {"time": "08:00", "viable": True},
            {"time": "08:45", "viable": True},
            {"time": "09:00", "viable": True},
            {"time": "18:00", "viable": True},
        ]
    }

    am_slots, _ = optimiser._get_viable_slots("Supreme HQ, Bandra")

    assert "08:30" in am_slots
    assert am_slots.index("08:30") < am_slots.index("09:00")


def test_derived_studio_viable_slots_add_reserve_pm_window_when_sparse():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.slot_availability = {
        "Copper & Cloves": [
            {"time": "09:00", "viable": True},
            {"time": "11:30", "viable": True},
        ]
    }

    am_slots, pm_slots = optimiser._get_viable_slots("Copper & Cloves")

    assert "09:00" in am_slots
    assert {"17:30", "18:00", "18:30"}.issubset(set(pm_slots))


def test_derived_studio_viable_slots_use_scored_historic_times_when_available():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.slot_availability = {
        "Copper & Cloves": [
            {"time": "09:00", "viable": True},
            {"time": "09:15", "viable": True},
            {"time": "11:30", "viable": True},
        ]
    }
    optimiser.scores_data = {
        "class_slot_ranking": [
            {"location": "Copper & Cloves", "day": 1, "time": "17:15", "class": "Copper + Cloves Barre 57", "trainer": "Chaitanya Nahar", "score": 25.45, "fill": 0.19, "sessions": 29},
            {"location": "Copper & Cloves", "day": 4, "time": "18:15", "class": "Copper + Cloves Barre 57", "trainer": "Siddhartha Kusuma", "score": 27.49, "fill": 0.16, "sessions": 23},
            {"location": "Copper & Cloves", "day": 5, "time": "09:00", "class": "Copper + Cloves Mat 57", "trainer": "Pushyank Nahar", "score": 55.88, "fill": 0.46, "sessions": 11},
        ]
    }
    optimiser._build_score_indexes()

    am_slots, pm_slots = optimiser._get_viable_slots("Copper & Cloves")

    assert "09:00" in am_slots
    assert "17:15" in pm_slots
    assert "18:15" in pm_slots


def test_mumbai_viable_slots_add_requested_parallel_peak_clusters():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.slot_availability = {
        "Kwality House, Kemps Corner": [
            {"time": "08:00", "viable": True},
            {"time": "11:30", "viable": True},
            {"time": "18:00", "viable": True},
        ],
        "Supreme HQ, Bandra": [
            {"time": "08:45", "viable": True},
            {"time": "18:15", "viable": True},
        ],
    }

    for location in ("Kwality House, Kemps Corner", "Supreme HQ, Bandra"):
        am_slots, pm_slots = optimiser._get_viable_slots(location)
        for time_str in ("08:00", "08:15", "08:30", "08:45", "11:00", "11:15", "11:30", "11:45"):
            assert time_str in am_slots
        for time_str in ("18:00", "18:15", "18:30", "18:45"):
            assert time_str in pm_slots
        assert pm_slots[:4] == ["18:00", "18:15", "18:30", "18:45"]


def test_ai_prompt_exposes_mumbai_parallel_peak_clusters():
    prompt = _build_location_prompt(
        "Supreme HQ, Bandra",
        "2026-05-04",
        {"class_slot_ranking": [{
            "location": "Kwality House, Kemps Corner",
            "day": "Monday",
            "time": "11:00",
            "class": "Studio Cardio Barre",
            "trainer": "Reshma Sharma",
            "fill": 0.20,
            "checkins": 3,
            "sessions": 8,
        }]},
        {"trainer_metrics": [], "day_band_metrics": []},
        profiles=[],
    )

    assert "MUMBAI PARALLEL PEAKS" in prompt
    assert "08:00/08:15/08:30/08:45" in prompt
    assert "11:00/11:15/11:30/11:45" in prompt
    assert "18:00/18:15/18:30/18:45" in prompt
    assert "Planning method: build the schedule as a constraint-satisfaction plan" in _build_system_prompt([], {"categories": {}, "groups": []})
    assert "self-check every slot" in _build_system_prompt([], {"categories": {}, "groups": []})
    assert "13-15h weekly operating band" in prompt


def test_ai_prompt_exposes_slots_for_derived_locations():
    prompt = _build_location_prompt(
        "Copper & Cloves",
        "2026-05-04",
        {"class_slot_ranking": []},
        {"trainer_metrics": [], "day_band_metrics": []},
        profiles=[],
    )

    assert "AM: 08:30" in prompt
    assert "PM: 17:30" in prompt


def test_location_weekly_class_bounds_include_requested_floors():
    assert LOCATION_WEEKLY_CLASS_BOUNDS["Kwality House, Kemps Corner"]["min"] == 70
    assert LOCATION_WEEKLY_CLASS_BOUNDS["Supreme HQ, Bandra"]["min"] == 65
    assert LOCATION_WEEKLY_CLASS_BOUNDS["Kenkere House"]["min"] == 55


def test_requested_format_trainer_priority_boosts_specific_trainers():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])

    assert optimiser._format_trainer_priority_score(
        "Vivaran Dhasmana", "Supreme HQ, Bandra", "Studio PowerCycle"
    ) > optimiser._format_trainer_priority_score(
        "Other Trainer", "Supreme HQ, Bandra", "Studio PowerCycle"
    )
    assert optimiser._format_trainer_priority_score(
        "Atulan Purohit", "Kwality House, Kemps Corner", "Studio Strength Lab"
    ) > optimiser._format_trainer_priority_score(
        "Other Trainer", "Kwality House, Kemps Corner", "Studio Strength Lab"
    )
    assert optimiser._format_trainer_priority_score(
        "Anisha Shah", "Kwality House, Kemps Corner", "Studio FIT"
    ) > optimiser._format_trainer_priority_score(
        "Other Trainer", "Kwality House, Kemps Corner", "Studio FIT"
    )


def test_ai_planner_retries_backup_model_when_primary_plan_is_invalid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "rules").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "state" / "03_scores.json").write_text(json.dumps({"class_slot_ranking": [], "slot_group_ranking": []}))
    (tmp_path / "state" / "02_metrics.json").write_text(json.dumps({"trainer_metrics": [], "day_band_metrics": []}))
    (tmp_path / "rules" / "trainer_profiles.json").write_text(json.dumps([]))
    (tmp_path / "config" / "rules_config.json").write_text(json.dumps({"categories": {"universal": {"enabled": True}}, "rules": {}}))
    calls = []

    import agents.ai_planner as ai_planner_module

    monkeypatch.setenv("SCHEDULER_FORCE_AI_ONLY", "1")
    monkeypatch.setattr(ai_planner_module, "OPENAI_AVAILABLE", True)
    monkeypatch.setattr(
        ai_planner_module,
        "create_ai_client",
        lambda: (object(), {"model": "primary-model", "backup_model": "z-ai/glm-4.5-air:free"}),
    )

    def fake_call(self, client, model_name, system_prompt, user_prompt, location, max_tokens=None):
        calls.append(model_name)
        return '{"schedule":[]}'

    valid_slots = [
        PlannedSlot(
            location="Kwality House, Kemps Corner",
            date="2026-05-04",
            day_of_week="Monday",
            time=f"08:{idx:02d}",
            class_name="Studio Barre 57",
            trainer_1="Trainer A",
            trainer_2="",
            cover="",
            room="studio_a",
            capacity=22,
            predicted_fill_rate=0.5,
            score=80,
            constraint_violations=[],
        )
        for idx in range(20)
    ]

    monkeypatch.setattr(AISchedulePlanner, "_call_model", fake_call)
    monkeypatch.setattr(ai_planner_module, "_minimum_ai_slot_count_for_location", lambda location: 20)
    monkeypatch.setattr(ai_planner_module, "_parse_schedule_response", lambda raw, location, week, profiles: (valid_slots, []) if calls[-1].startswith("z-ai/") else ([], []))
    monkeypatch.setattr(ai_planner_module, "_validate_slots", lambda slots, location, profiles: slots)
    monkeypatch.setattr(ai_planner_module, "_enforce_hard_limits", lambda slots, location, profiles: slots)
    monkeypatch.setattr(ai_planner_module, "_has_enough_slots_after_enforcement", lambda location, slots: bool(slots))
    monkeypatch.setattr(ai_planner_module, "_score_slots", lambda slots, scores: slots)
    monkeypatch.setattr(ai_planner_module, "_enforce_global_trainer_overlaps", lambda slots, profiles: slots)

    planner = AISchedulePlanner(target_week_start="2026-05-04", locations=["Kwality House, Kemps Corner"])
    output = planner.run()

    assert calls == ["primary-model", "z-ai/glm-4.5-air:free"]
    assert output["ai_models"] == ["primary-model", "z-ai/glm-4.5-air:free"]
    assert len(output["schedule"]) == 20


def test_ai_planner_retries_backup_model_when_primary_plan_is_partial(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "rules").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "state" / "03_scores.json").write_text(json.dumps({"class_slot_ranking": [], "slot_group_ranking": []}))
    (tmp_path / "state" / "02_metrics.json").write_text(json.dumps({"trainer_metrics": [], "day_band_metrics": []}))
    (tmp_path / "rules" / "trainer_profiles.json").write_text(json.dumps([]))
    (tmp_path / "config" / "rules_config.json").write_text(json.dumps({"categories": {"universal": {"enabled": True}}, "rules": {}}))
    calls = []

    import agents.ai_planner as ai_planner_module

    monkeypatch.setenv("SCHEDULER_FORCE_AI_ONLY", "1")
    monkeypatch.setattr(ai_planner_module, "OPENAI_AVAILABLE", True)
    monkeypatch.setattr(
        ai_planner_module,
        "create_ai_client",
        lambda: (object(), {"model": "primary-model", "backup_model": "z-ai/glm-4.5-air:free"}),
    )
    monkeypatch.setattr(ai_planner_module, "_minimum_ai_slot_count_for_location", lambda location: 9)

    def fake_call(self, client, model_name, system_prompt, user_prompt, location, max_tokens=None):
        calls.append(model_name)
        return '{"schedule":[]}'

    partial_slots = [
        PlannedSlot(
            location="Copper & Cloves",
            date="2026-05-04",
            day_of_week="Monday",
            time=f"09:{idx:02d}",
            class_name="Copper + Cloves Barre 57",
            trainer_1="Trainer A",
            trainer_2="",
            cover="",
            room="studio_a",
            capacity=14,
            predicted_fill_rate=0.5,
            score=80,
            constraint_violations=[],
        )
        for idx in range(5)
    ]
    valid_slots = [
        PlannedSlot(
            location="Copper & Cloves",
            date="2026-05-04",
            day_of_week="Monday",
            time=f"10:{idx:02d}",
            class_name="Copper + Cloves Barre 57",
            trainer_1="Trainer A",
            trainer_2="",
            cover="",
            room="studio_a",
            capacity=14,
            predicted_fill_rate=0.5,
            score=80,
            constraint_violations=[],
        )
        for idx in range(9)
    ]

    monkeypatch.setattr(AISchedulePlanner, "_call_model", fake_call)
    monkeypatch.setattr(
        ai_planner_module,
        "_parse_schedule_response",
        lambda raw, location, week, profiles: (valid_slots, []) if calls[-1].startswith("z-ai/") else (partial_slots, []),
    )
    monkeypatch.setattr(ai_planner_module, "_validate_slots", lambda slots, location, profiles: slots)
    monkeypatch.setattr(ai_planner_module, "_enforce_hard_limits", lambda slots, location, profiles: slots)
    monkeypatch.setattr(ai_planner_module, "_score_slots", lambda slots, scores: slots)
    monkeypatch.setattr(ai_planner_module, "_enforce_global_trainer_overlaps", lambda slots, profiles: slots)

    planner = AISchedulePlanner(target_week_start="2026-05-04", locations=["Copper & Cloves"])
    output = planner.run()

    assert calls == ["primary-model", "z-ai/glm-4.5-air:free"]
    assert len(output["schedule"]) == 9
    assert output["ai_repaired_locations"] == []


def test_ai_location_parallelism_defaults_to_one_for_free_models(monkeypatch):
    import agents.ai_planner as ai_planner_module

    monkeypatch.delenv("AI_LOCATION_PARALLELISM", raising=False)

    assert ai_planner_module._location_parallelism([
        "openai/gpt-oss-120b:free",
        "z-ai/glm-4.5-air:free",
    ], location_count=5) == 1
    assert ai_planner_module._location_parallelism(["gpt-4o-mini"], location_count=5) == 3
    assert ai_planner_module._location_parallelism([
        "deepseek-v4-flash",
        "openai/gpt-oss-120b:free",
    ], location_count=5) == 3


def test_ai_location_parallelism_env_override_is_clamped(monkeypatch):
    import agents.ai_planner as ai_planner_module

    monkeypatch.setenv("AI_LOCATION_PARALLELISM", "9")

    assert ai_planner_module._location_parallelism(["openai/gpt-oss-120b:free"], location_count=5) == 5


def test_ai_completion_uses_request_timeout(monkeypatch):
    import ai_provider

    captured = {}
    monkeypatch.setattr(ai_provider, "get_ai_settings", lambda: None)

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return object()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    monkeypatch.setenv("AI_REQUEST_TIMEOUT_SECONDS", "12")
    ai_provider.create_chat_completion(
        client=_Client(),
        system_prompt="system",
        user_prompt="user",
        model="test-model",
        max_tokens=123,
    )

    assert captured["model"] == "test-model"
    assert captured["max_tokens"] == 123
    assert captured["timeout"] == 12


def test_ai_location_token_budget_scales_with_target(monkeypatch):
    import agents.ai_planner as ai_planner_module

    monkeypatch.setattr(ai_planner_module, "_target_count_for_location", lambda loc: 4 if loc == "small" else 70)

    assert ai_planner_module._max_tokens_for_location("small") == 1800
    assert ai_planner_module._max_tokens_for_location("large") == ai_planner_module.MAX_TOKENS


def test_ai_small_studio_target_accepts_exact_sized_plan(monkeypatch):
    import agents.ai_planner as ai_planner_module

    monkeypatch.setattr(ai_planner_module, "_target_count_for_location", lambda loc: 9 if loc == "Copper & Cloves" else 70)

    small_slots = [
        PlannedSlot(
            location="Copper & Cloves",
            date="2026-05-04",
            day_of_week="Monday",
            time=f"09:{idx:02d}",
            class_name="Copper + Cloves Barre 57",
            trainer_1="Trainer A",
            trainer_2="",
            cover="",
            room="studio_a",
            capacity=14,
            predicted_fill_rate=0.5,
            score=80,
            constraint_violations=[],
        )
        for idx in range(9)
    ]
    large_slots = [
        PlannedSlot(
            location="Kwality House, Kemps Corner",
            date="2026-05-04",
            day_of_week="Monday",
            time=f"09:{idx:02d}",
            class_name="Studio Barre 57",
            trainer_1="Trainer A",
            trainer_2="",
            cover="",
            room="studio_a",
            capacity=22,
            predicted_fill_rate=0.5,
            score=80,
            constraint_violations=[],
        )
        for idx in range(20)
    ]

    assert ai_planner_module._has_enough_slots_after_enforcement("Copper & Cloves", small_slots)
    assert not ai_planner_module._has_enough_slots_after_enforcement("Kwality House, Kemps Corner", large_slots)


def test_horizontal_mix_blocks_third_same_class_at_same_time():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    key = ("Supreme HQ, Bandra", "09:00", "Studio Barre 57")
    optimiser._time_class_counts[key] = 2

    assert not optimiser._horizontal_mix_allows_candidate(
        "Supreme HQ, Bandra",
        "09:00",
        "Studio Barre 57",
    )


def test_protected_class_variant_does_not_collapse_express_to_base():
    assert same_protected_class_variant("Studio Barre 57 Express", "Studio Barre 57 Express")
    assert not same_protected_class_variant("Studio Barre 57 Express", "Studio Barre 57")
    assert not same_protected_class_variant("Studio Barre 57", "Studio Barre 57 Express")


def test_class_durations_match_class_format_config():
    with open("rules/class_formats.json") as f:
        formats = json.load(f)

    mismatches = {
        row["name"]: (row["duration_min"], get_class_duration(row["name"]))
        for row in formats
        if row.get("duration_min") != get_class_duration(row["name"])
    }

    assert mismatches == {}


def test_ai_parser_keeps_parallel_same_time_slots_and_sets_duration():
    raw = json.dumps({
        "schedule": [
            {
                "day": "Monday",
                "time": "09:00",
                "class": "Studio Cardio Barre Plus",
                "trainer": "Trainer One",
                "cover": "Cover One",
            },
            {
                "day": "Monday",
                "time": "09:00",
                "class": "Studio Mat 57 Express",
                "trainer": "Trainer Two",
                "cover": "Cover Two",
            },
        ]
    })

    slots, errors = _parse_schedule_response(
        raw,
        "Kwality House, Kemps Corner",
        "2026-05-04",
        profiles_by_name={},
    )

    assert errors == []
    assert len(slots) == 2
    assert [slot.duration_min for slot in slots] == [75, 30]
    assert {slot.room for slot in slots} == {"studio_a", "studio_b"}
    assert [slot.capacity for slot in slots] == [22, 13]


def test_ai_hard_limits_respect_configured_strength_lab_weekly_max(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    (Path("config") / "schedule_config.json").write_text(json.dumps({
        "class_mix": {
            "Kwality House, Kemps Corner": {
                "Studio Strength Lab": {"min": 2, "max": 4}
            }
        }
    }))

    classes = [
        "Studio Barre 57",
        "Studio Mat 57",
        "Studio Cardio Barre",
        "Studio FIT",
        "Studio PowerCycle",
    ]
    slots = [
        PlannedSlot(
            location="Kwality House, Kemps Corner",
            date=f"2026-05-{4 + idx:02d}",
            day_of_week=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"][idx],
            time=["18:00", "18:15", "18:30", "18:45", "19:00"][idx],
            class_name="Studio Strength Lab",
            trainer_1="Atulan Purohit",
            trainer_2="",
            cover="",
            room="strength_lab",
            capacity=7,
            duration_min=57,
            predicted_fill_rate=0.5,
            score=70.0,
            constraint_violations=[],
        )
        for idx in range(5)
    ]

    kept = _enforce_hard_limits(slots, "Kwality House, Kemps Corner", profiles_by_name={})

    assert len(kept) == 4


def test_flask_chat_endpoint_returns_reply(monkeypatch):
    monkeypatch.setattr(flask_app_module, "_build_chat_reply", lambda payload: "stub reply", raising=False)

    response = flask_app_module.app.test_client().post(
        "/api/chat",
        json={"message": "What should I change at Kwality?", "history": []},
    )

    assert response.status_code == 200
    assert response.get_json()["reply"] == "stub reply"


def test_chat_substitution_questions_are_answered_by_llm(monkeypatch):
    import ai_provider

    calls = {}

    class FakeCompletions:
        def create(self, **kwargs):
            calls["kwargs"] = kwargs

            class Message:
                content = "LLM substitution answer"

            class Choice:
                message = Message()

            class Response:
                choices = [Choice()]

            return Response()

    class FakeClient:
        class Chat:
            completions = FakeCompletions()

        chat = Chat()

    monkeypatch.setattr(ai_provider, "OPENAI_AVAILABLE", True)
    monkeypatch.setattr(ai_provider, "create_ai_client", lambda: (FakeClient(), {"model": "test-model"}))
    assert not hasattr(flask_app_module, "answer_substitution_request")

    reply = flask_app_module._build_chat_reply({
        "message": "substitute for Wednesday 7:30 strength at Kwality",
        "history": [],
    })

    assert reply == "LLM substitution answer"
    assert calls["kwargs"]["model"] == "test-model"
    assert calls["kwargs"]["messages"][-1]["content"] == "substitute for Wednesday 7:30 strength at Kwality"
    system_context = calls["kwargs"]["messages"][0]["content"]
    assert "RELEVANT SCHEDULE EVIDENCE" in system_context
    assert "Wednesday 07:30" in system_context
    assert "Studio Strength Lab" in system_context
    assert "Trainer same-day assignments" in system_context
    assert "Potential trainer evidence" in system_context
    assert "overlap_conflicts=" in system_context
    assert "weekly_hours_if_added=" in system_context
    assert "STRUCTURED RECOMMENDATION FORMAT" in system_context
    assert "Best option:" in system_context
    assert "Avoid:" in system_context
    assert "Why:" in system_context
    assert "Next action:" in system_context
    assert "RANKED SUBSTITUTION CANDIDATES" in system_context
    assert "recommendation_status=eligible" in system_context
    assert "recommendation_status=blocked" in system_context


def test_optimize_schedule_applies_validated_ai_patch(tmp_path, monkeypatch):
    monkeypatch.setattr(flask_app_module, "WEB_DIR", tmp_path)
    schedule_path = tmp_path / "schedule_data.json"
    schedule_path.write_text(json.dumps({
        "locations": {
            "Supreme HQ, Bandra": [
                {
                    "location": "Supreme HQ, Bandra",
                    "date": "2026-05-04",
                    "day_of_week": "Monday",
                    "time": "09:00",
                    "class_name": "Studio Barre 57",
                    "trainer_1": "Trainer A",
                    "trainer_2": "",
                    "cover": "",
                    "room": "studio_a",
                    "capacity": 14,
                    "duration_min": 57,
                    "score": 20,
                    "predicted_fill_rate": 0.2,
                }
            ]
        }
    }))
    monkeypatch.setattr(flask_app_module, "_call_schedule_optimizer_ai", lambda payload: {
        "summary": "Improve Supreme morning quality.",
        "operations": [
            {
                "type": "swap_trainer",
                "reason": "Trainer B has stronger history.",
                "slot": {
                    "location": "Supreme HQ, Bandra",
                    "day_of_week": "Monday",
                    "time": "09:00",
                    "class_name": "Studio Barre 57",
                    "trainer_1": "Trainer A",
                },
                "new_trainer": "Trainer B",
            }
        ],
    }, raising=False)
    monkeypatch.setattr(flask_app_module, "_validate_manual_slot", lambda data, iteration, slot, original_slot=None: None)
    monkeypatch.setattr(flask_app_module, "_save_schedule_to_supabase", lambda data: {"saved": False})
    monkeypatch.setattr(flask_app_module, "_regenerate_index_from_template", lambda data=None: None)

    response = flask_app_module.app.test_client().post("/api/optimize-schedule", json={"iteration": "Main"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["applied_count"] == 1
    assert payload["rejected_count"] == 0
    updated = json.loads(schedule_path.read_text())
    slot = updated["locations"]["Supreme HQ, Bandra"][0]
    assert slot["trainer_1"] == "Trainer B"
    assert slot["ai_optimized"] is True


def test_optimizer_prompt_requires_json_patch_and_validation_language(monkeypatch):
    monkeypatch.setattr(flask_app_module, "_latest_schedule_payload", lambda: {
        "locations": {
            "Supreme HQ, Bandra": [
                {
                    "location": "Supreme HQ, Bandra",
                    "day_of_week": "Monday",
                    "time": "09:00",
                    "class_name": "Studio Barre 57",
                    "trainer_1": "Trainer A",
                }
            ]
        }
    })

    prompt = flask_app_module._build_schedule_optimizer_prompt({"iteration": "Main", "location": "Supreme HQ, Bandra"})

    assert "Return JSON only" in prompt
    assert "swap_trainer" in prompt
    assert "remove_class" in prompt
    assert "move_class" in prompt
    assert "add_class" in prompt
    assert "change_class" in prompt
    assert "Every operation will be server-validated" in prompt
    assert "Supreme HQ, Bandra" in prompt


def test_chat_context_includes_active_dashboard_context(tmp_path):
    from chat_assistant import build_chat_context

    schedule = tmp_path / "schedule_data.json"
    scorecard = tmp_path / "scorecard.json"
    profiles = tmp_path / "profiles.json"
    schedule.write_text(json.dumps({
        "locations": {
            "Supreme HQ, Bandra": [
                {
                    "day_of_week": "Monday",
                    "time": "09:00",
                    "class_name": "Studio Barre 57",
                    "trainer_1": "Trainer A",
                }
            ]
        }
    }))
    scorecard.write_text(json.dumps({}))
    profiles.write_text(json.dumps([]))

    context = build_chat_context(
        schedule,
        scorecard,
        profiles,
        "optimize supreme",
        dashboard_context={"location": "Supreme HQ, Bandra", "mode": "Analyze", "iteration": "Main"},
    )

    assert "ACTIVE DASHBOARD CONTEXT" in context
    assert "Supreme HQ, Bandra" in context
    assert "Analyze" in context


def test_dashboard_has_ai_optimize_button_and_client_handler():
    template = web_ui_source()

    assert 'id="optimize-ai-btn"' in template
    assert "function optimizeScheduleWithAI" in template
    assert 'fetch("/api/optimize-schedule"' in template


def test_chat_ui_has_advanced_modes_and_context_payload():
    template = web_ui_source()

    assert "chat-mode-tabs" in template
    assert "data-chat-mode" in template
    assert "dashboard_context" in template
    assert "Analyze" in template
    assert "Optimize Ideas" in template


def test_chat_context_includes_relevant_schedule_rows_for_specific_question():
    from chat_assistant import build_chat_context

    context = build_chat_context(
        Path("web/schedule_data.json"),
        Path("outputs/scorecard.json"),
        Path("rules/trainer_profiles.json"),
        "substitute for Wednesday 7:30 strength at Kwality",
    )

    assert "RELEVANT SCHEDULE EVIDENCE" in context
    assert "Requested slot: Wednesday 07:30" in context
    assert "Studio Strength Lab" in context
    assert "Trainer same-day assignments" in context
    assert "Potential trainer evidence" in context
    assert "overlap_conflicts=" in context
    assert "same_location_same_shift_nonoverlap=" in context
    assert "sessions_if_added=" in context
    assert "weekly_hours_if_added=" in context
    assert "matching_track_record=" in context
    assert "RANKED SUBSTITUTION CANDIDATES" in context
    assert "rank=1" in context
    assert "recommendation_status=eligible" in context
    assert "recommendation_status=blocked" in context
    assert "blocked_reasons=" in context
    assert "Mrigakshi Jaiswal" in context
    assert "Richard D'Costa" in context


def test_chat_context_includes_structured_advisor_format_and_intent():
    from chat_assistant import build_chat_context

    context = build_chat_context(
        Path("web/schedule_data.json"),
        Path("outputs/scorecard.json"),
        Path("rules/trainer_profiles.json"),
        "Which classes have the lowest fill rate at Kwality and what should we fix?",
    )

    assert "DETECTED QUESTION INTENT: low_fill" in context
    assert "STRUCTURED RECOMMENDATION FORMAT" in context
    assert "Best option:" in context
    assert "Avoid:" in context
    assert "Why:" in context
    assert "Next action:" in context
    assert "LOW-FILL / IMPROVEMENT EVIDENCE" in context
    assert "lowest_fill_classes=" in context


def test_chat_context_includes_decision_evidence_for_explain_questions():
    from chat_assistant import build_chat_context

    context = build_chat_context(
        Path("web/schedule_data.json"),
        Path("outputs/scorecard.json"),
        Path("rules/trainer_profiles.json"),
        "Explain class Wednesday 7:30 strength at Kwality",
    )

    assert "DETECTED QUESTION INTENT: explain" in context
    assert "DECISION EXPLANATION EVIDENCE" in context
    assert "scheduling_reason=" in context
    assert "recommendation=" in context
    assert "constraint_violations=" in context


def test_chat_ui_exposes_advanced_quick_actions():
    template = web_ui_source()

    assert "Find substitute" in template
    assert "Explain class" in template
    assert "Workload check" in template
    assert "Add class idea" in template


def test_ai_validation_does_not_enforce_disabled_atulan_strength_exclusivity():
    slot = PlannedSlot(
        location="Kwality House, Kemps Corner",
        date="2026-05-13",
        day_of_week="Wednesday",
        time="07:30",
        class_name="Studio Strength Lab (Pull)",
        trainer_1="Mrigakshi Jaiswal",
        trainer_2="",
        cover="",
        room="strength_lab",
        capacity=7,
        duration_min=57,
        predicted_fill_rate=0.5,
        score=70.0,
        constraint_violations=[],
    )

    profiles = {
        "Mrigakshi Jaiswal": {
            "name": "Mrigakshi Jaiswal",
            "active": True,
            "locations": {
                "Kwality House, Kemps Corner": {
                    "available_days": ["Wednesday"],
                    "max_classes_per_day": 4,
                }
            },
        }
    }

    validated = _validate_slots([slot], "Kwality House, Kemps Corner", profiles)

    assert not any("KW-006" in item for item in validated[0].constraint_violations)


def test_ai_hard_limit_enforcer_drops_trainer_duration_overlap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    Path("config/schedule_config.json").write_text(json.dumps({}))

    slots = [
        make_slot(
            time="08:00",
            class_name="Studio Mat 57",
            trainer_1="Trainer A",
            duration_min=get_class_duration("Studio Mat 57"),
        ),
        make_slot(
            time="08:45",
            class_name="Studio Barre 57",
            trainer_1="Trainer A",
            duration_min=get_class_duration("Studio Barre 57"),
        ),
        make_slot(
            time="09:00",
            class_name="Studio Barre 57",
            trainer_1="Trainer B",
            duration_min=get_class_duration("Studio Barre 57"),
        ),
    ]
    profiles = {
        "Trainer A": {
            "name": "Trainer A",
            "active": True,
            "locations": {
                "Kwality House, Kemps Corner": {
                    "available_days": ["Monday"],
                    "time_window": {"start": "07:00", "end": "12:00"},
                    "max_classes_per_day": 4,
                }
            },
        },
        "Trainer B": {
            "name": "Trainer B",
            "active": True,
            "locations": {
                "Kwality House, Kemps Corner": {
                    "available_days": ["Monday"],
                    "time_window": {"start": "07:00", "end": "12:00"},
                    "max_classes_per_day": 4,
                }
            },
        },
    }

    kept = _enforce_hard_limits(slots, "Kwality House, Kemps Corner", profiles)

    assert [(s.trainer_1, s.time) for s in kept] == [("Trainer A", "08:00"), ("Trainer B", "09:00")]


def test_ai_hard_limit_enforcer_keeps_at_least_one_trainer_rest_day(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    Path("config/schedule_config.json").write_text(json.dumps({}))

    slots = [
        make_slot(
            day_of_week=day,
            date=f"2026-05-{4 + idx:02d}",
            time=["10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30"][idx],
            trainer_1="Trainer A",
            duration_min=57,
        )
        for idx, day in enumerate(DAY_ORDER)
    ]
    profiles = {
        "Trainer A": {
            "name": "Trainer A",
            "active": True,
            "locations": {
                "Kwality House, Kemps Corner": {
                    "available_days": DAY_ORDER,
                    "time_window": {"start": "07:00", "end": "12:00"},
                    "max_classes_per_day": 4,
                }
            },
        }
    }

    kept = _enforce_hard_limits(slots, "Kwality House, Kemps Corner", profiles)

    # CLAUDE.md cap: max 5 work days; planner must drop down to <=5 distinct days.
    assert len({slot.day_of_week for slot in kept}) == 5


def test_ai_hard_limit_enforcer_clears_stale_constraint_badges(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    Path("config/schedule_config.json").write_text(json.dumps({}))

    slot = make_slot(
        trainer_1="Trainer A",
        constraint_violations=["UNIV-010: stale daily limit warning"],
    )
    profiles = {
        "Trainer A": {
            "name": "Trainer A",
            "active": True,
            "locations": {
                "Kwality House, Kemps Corner": {
                    "available_days": ["Monday"],
                    "time_window": {"start": "07:00", "end": "12:00"},
                    "max_classes_per_day": 4,
                }
            },
        }
    }

    kept = _enforce_hard_limits([slot], "Kwality House, Kemps Corner", profiles)

    assert len(kept) == 1
    assert kept[0].constraint_violations == []


def test_ai_hard_limit_enforcer_respects_settings_off_days(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    Path("config/schedule_config.json").write_text(json.dumps({
        "off_days": [{"trainer": "Trainer A", "date": "2026-05-04", "location": "Kwality House, Kemps Corner"}]
    }))

    slot = make_slot(
        date="2026-05-04",
        day_of_week="Monday",
        trainer_1="Trainer A",
    )
    profiles = {
        "Trainer A": {
            "name": "Trainer A",
            "active": True,
            "locations": {
                "Kwality House, Kemps Corner": {
                    "available_days": ["Monday"],
                    "time_window": {"start": "07:00", "end": "12:00"},
                    "max_classes_per_day": 4,
                }
            },
        }
    }

    kept = _enforce_hard_limits([slot], "Kwality House, Kemps Corner", profiles)

    assert kept == []


def test_ai_hard_limit_enforcer_respects_custom_trainer_availability(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    Path("config/schedule_config.json").write_text(json.dumps({
        "custom_rules": [
            {
                "rule_type": "trainer_availability",
                "trainer": "Trainer A",
                "location": "Kwality House, Kemps Corner",
                "day": "Tuesday",
                "operator": "only",
                "priority": "hard",
                "enabled": True,
            }
        ]
    }))

    monday_slot = make_slot(
        date="2026-05-04",
        day_of_week="Monday",
        trainer_1="Trainer A",
    )
    profiles = {
        "Trainer A": {
            "name": "Trainer A",
            "active": True,
            "locations": {
                "Kwality House, Kemps Corner": {
                    "available_days": DAY_ORDER,
                    "time_window": {"start": "07:00", "end": "12:00"},
                    "max_classes_per_day": 4,
                }
            },
        }
    }

    kept = _enforce_hard_limits([monday_slot], "Kwality House, Kemps Corner", profiles)

    assert kept == []


def test_greedy_protected_slots_respect_trainer_available_days():
    location = "Kwality House, Kemps Corner"
    optimiser = ScheduleOptimiser(target_week_start="2026-05-11", locations=[])
    optimiser.trainer_profiles = {
        "Anisha Shah": {
            "name": "Anisha Shah",
            "tier": 1,
            "locations": {
                location: {
                    "available_days": ["Monday", "Tuesday", "Wednesday"],
                    "time_window": {"start": "07:00", "end": "12:00"},
                    "max_classes_per_day": 4,
                }
            },
            "qualifications": {"all_barre": True},
        }
    }
    optimiser.trainer_states = {"Anisha Shah": TrainerState("Anisha Shah", 1)}
    optimiser.class_family = {"Studio Barre 57": "barre_57"}
    optimiser.schedule_config = {}
    optimiser.overrides = {}
    optimiser.protected = {
        (location, DAY_ORDER.index("Friday")): [
            {
                "time": "10:00",
                "trainer": "Anisha Shah",
                "class": "Studio Barre 57",
                "score": 90,
                "score_breakdown": {},
                "session_count": 10,
            }
        ]
    }
    optimiser.protected_class_times = {}
    optimiser.scores_data = {"class_slot_ranking": []}
    optimiser._build_score_indexes()
    optimiser.hist_lookup = {}
    optimiser._build_history_indexes()
    optimiser._pinned_minutes_remaining = {}
    optimiser._time_class_counts = {}
    optimiser._time_level_counts = {}

    slots = optimiser._schedule_day(
        location,
        "Friday",
        "2026-05-15",
        0,
        0,
        [],
        [],
        RoomOccupancy({"studio_a": {"capacity": 22, "families": None}}),
        {},
    )

    assert slots == []


def test_greedy_experimental_slots_respect_trainer_available_days():
    location = "Supreme HQ, Bandra"
    optimiser = ScheduleOptimiser(target_week_start="2026-05-11", locations=[])
    optimiser.trainer_profiles = {
        "Anisha Shah": {
            "name": "Anisha Shah",
            "tier": 1,
            "locations": {
                location: {
                    "available_days": ["Thursday"],
                    "time_window": {"start": "08:00", "end": "12:30"},
                    "max_classes_per_day": 4,
                }
            },
            "qualifications": {"all_barre": True, "powercycle": True},
        }
    }
    optimiser.trainer_states = {"Anisha Shah": TrainerState("Anisha Shah", 1)}
    optimiser.class_family = {"Studio Barre 57": "barre_57"}
    optimiser.schedule_config = {}
    optimiser.overrides = {}
    optimiser._pinned_minutes_remaining = {}

    assert not optimiser._trainer_ok(
        "Anisha Shah",
        location,
        "Saturday",
        "09:00",
        "Studio Barre 57",
        experimental=True,
    )


def test_trainer_ok_respects_custom_only_assignment_day():
    location = "Kwality House, Kemps Corner"
    optimiser = ScheduleOptimiser(target_week_start="2026-05-11", locations=[])
    optimiser.trainer_profiles = {
        "Custom Trainer": {
            "name": "Custom Trainer",
            "tier": 2,
            "locations": {
                location: {
                    "available_days": DAY_ORDER,
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 4,
                }
            },
            "qualifications": {"all_barre": True},
        }
    }
    optimiser.trainer_states = {"Custom Trainer": TrainerState("Custom Trainer", 2)}
    optimiser.overrides = {}
    optimiser.schedule_config = {
        "custom_rules": [
            {
                "rule_type": "trainer_availability",
                "trainer": "Custom Trainer",
                "location": location,
                "day": "Tuesday",
                "operator": "only",
                "priority": "hard",
                "enabled": True,
            }
        ]
    }
    optimiser._pinned_minutes_remaining = {}

    assert not optimiser._trainer_ok("Custom Trainer", location, "Wednesday", "09:00", "Studio Barre 57")
    assert optimiser._trainer_ok("Custom Trainer", location, "Tuesday", "09:00", "Studio Barre 57")


def test_trainer_ok_respects_settings_off_day_date():
    location = "Kwality House, Kemps Corner"
    optimiser = ScheduleOptimiser(target_week_start="2026-05-11", locations=[])
    optimiser.trainer_profiles = {
        "Custom Trainer": {
            "name": "Custom Trainer",
            "tier": 2,
            "locations": {
                location: {
                    "available_days": DAY_ORDER,
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 4,
                }
            },
            "qualifications": {"all_barre": True},
        }
    }
    optimiser.trainer_states = {"Custom Trainer": TrainerState("Custom Trainer", 2)}
    optimiser.overrides = {
        "off_days": [{"trainer": "Custom Trainer", "date": "2026-05-12", "location": location}]
    }
    optimiser.schedule_config = {"custom_rules": []}
    optimiser._pinned_minutes_remaining = {}

    assert not optimiser._trainer_ok("Custom Trainer", location, "Tuesday", "09:00", "Studio Barre 57")


def test_trainer_ok_respects_custom_trainer_load_limit():
    location = "Kwality House, Kemps Corner"
    optimiser = ScheduleOptimiser(target_week_start="2026-05-11", locations=[])
    optimiser.trainer_profiles = {
        "Custom Trainer": {
            "name": "Custom Trainer",
            "tier": 2,
            "locations": {
                location: {
                    "available_days": DAY_ORDER,
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 4,
                }
            },
            "qualifications": {"all_barre": True},
        }
    }
    state = TrainerState("Custom Trainer", 2)
    state.add("Monday", "09:00", location, "Studio Barre 57")
    state.add("Tuesday", "09:00", location, "Studio Barre 57")
    optimiser.trainer_states = {"Custom Trainer": state}
    optimiser.overrides = {}
    optimiser.schedule_config = {
        "custom_rules": [
            {
                "rule_type": "trainer_load_limit",
                "trainer": "Custom Trainer",
                "location": location,
                "operator": "max_classes",
                "value": 2,
                "priority": "hard",
                "enabled": True,
            }
        ]
    }
    optimiser._pinned_minutes_remaining = {}

    assert not optimiser._trainer_ok("Custom Trainer", location, "Wednesday", "09:00", "Studio Barre 57")


def test_trainer_ok_respects_custom_blocked_time_window():
    location = "Kwality House, Kemps Corner"
    optimiser = ScheduleOptimiser(target_week_start="2026-05-11", locations=[])
    optimiser.trainer_profiles = {
        "Custom Trainer": {
            "name": "Custom Trainer",
            "tier": 2,
            "locations": {
                location: {
                    "available_days": DAY_ORDER,
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 4,
                }
            },
            "qualifications": {"all_barre": True},
        }
    }
    optimiser.trainer_states = {"Custom Trainer": TrainerState("Custom Trainer", 2)}
    optimiser.overrides = {}
    optimiser.schedule_config = {
        "custom_rules": [
            {
                "rule_type": "time_window_rule",
                "location": location,
                "day": "Tuesday",
                "time": "08:00",
                "time_end": "10:00",
                "operator": "block_window",
                "priority": "hard",
                "enabled": True,
            }
        ]
    }
    optimiser._pinned_minutes_remaining = {}

    assert not optimiser._trainer_ok("Custom Trainer", location, "Tuesday", "09:00", "Studio Barre 57")
    assert optimiser._trainer_ok("Custom Trainer", location, "Tuesday", "10:30", "Studio Barre 57")


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
            duration_min=57,
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
            duration_min=57,
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


def test_reporter_uses_canonical_slot_history_when_exact_card_metrics_are_missing(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    web_dir = tmp_path / "web"
    state_dir.mkdir()
    web_dir.mkdir()
    monkeypatch.setattr(reporter_module, "STATE_DIR", state_dir)
    monkeypatch.setattr(reporter_module, "WEB_DIR", web_dir)
    (state_dir / "03_scores.json").write_text(json.dumps({
        "class_slot_ranking": [],
        "slot_group_ranking": [],
    }))
    (state_dir / "01_sessions.json").write_text(json.dumps({
        "sessions": [
            {
                "Location": "Kwality House, Kemps Corner",
                "Class": "Studio Barre 57 Express",
                "Trainer": "Rohan Dahima",
                "Day": "Monday",
                "Time": "17:45:00",
                "Date": "2024-02-19",
                "Capacity": 22,
                "CheckedIn": 11,
                "Booked": 11,
                "Revenue": 10020.36,
                "LateCancelled": 0,
                "late_cancel_rate": 0,
                "no_show_rate": 0,
                "UniqueID1": "V4VCU8Z",
                "UniqueID2": "93RNH2E",
            },
            {
                "Location": "Kwality House, Kemps Corner",
                "Class": "Studio Barre 57 Express",
                "Trainer": "Janhavi Jain",
                "Day": "Monday",
                "Time": "17:45:00",
                "Date": "2024-02-12",
                "Capacity": 22,
                "CheckedIn": 5,
                "Booked": 5,
                "Revenue": 4039.58,
                "LateCancelled": 0,
                "late_cancel_rate": 0,
                "no_show_rate": 0,
                "UniqueID1": "V4VCU8Z",
                "UniqueID2": "SZN8YDJ",
            },
        ]
    }))
    slot = make_slot(
        location="Kwality House, Kemps Corner",
        class_name="Studio Barre 57",
        trainer_1="Cauveri Vikrant",
        day_of_week="Monday",
        time="17:45",
        historical_session_count=0,
        historical_avg_checkin=0,
        historical_avg_fill=0,
    ).__dict__

    OutputReporter()._write_schedule_data(
        {"Kwality House, Kemps Corner": [slot]},
        "2026-05-04",
        {"trainer_metrics": []},
    )

    data = json.loads((web_dir / "schedule_data.json").read_text())
    enriched = data["locations"]["Kwality House, Kemps Corner"][0]
    assert enriched["metric_source"] == "canonical_class_day_time_location"
    assert enriched["metric_session_count"] == 2
    assert enriched["metric_avg_checkin"] == 8.0
    assert enriched["slot_avg_checkin"] is None
    assert enriched["slot_session_count"] is None
    assert enriched["slot_historic_detail"]["session_rows"] == 2
    assert len(enriched["slot_historic_detail"]["individual_sessions"]) == 2


def test_reporter_uses_nearby_same_class_score_history_when_drilldown_would_find_rows(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    web_dir = tmp_path / "web"
    state_dir.mkdir()
    web_dir.mkdir()
    monkeypatch.setattr(reporter_module, "STATE_DIR", state_dir)
    monkeypatch.setattr(reporter_module, "WEB_DIR", web_dir)
    historic_detail = {
        "session_rows": 3,
        "avg_checked_in": 6.0,
        "avg_booked": 7.0,
        "avg_capacity": 20.0,
        "avg_fill_rate": 0.30,
        "avg_revenue": 1200.0,
        "total_revenue": 3600.0,
        "individual_sessions": [
            {
                "date": "2025-01-06",
                "trainer": "Trainer A",
                "class": "Studio Barre 57",
                "location": "Kwality House, Kemps Corner",
                "day": "Monday",
                "time": "10:15",
                "checked_in": 6,
                "booked": 7,
                "capacity": 20,
                "fill_rate": 0.30,
                "revenue": 1200,
            }
        ],
    }
    (state_dir / "03_scores.json").write_text(json.dumps({
        "class_slot_ranking": [],
        "slot_group_ranking": [
            {
                "location": "Kwality House, Kemps Corner",
                "class": "Studio Barre 57",
                "day_name": "Monday",
                "time": "10:15",
                "session_count": 3,
                "avg_attendance": 6.0,
                "avg_checkin": 6.0,
                "avg_fill_rate": 0.30,
                "avg_revenue": 1200.0,
                "score": 67.0,
                "historic_detail": historic_detail,
            },
            {
                "location": "Kwality House, Kemps Corner",
                "class": "Studio HIIT",
                "day_name": "Monday",
                "time": "09:30",
                "session_count": 9,
                "avg_attendance": 15.0,
                "avg_checkin": 15.0,
                "avg_fill_rate": 0.75,
                "score": 95.0,
                "historic_detail": {"session_rows": 9, "avg_checked_in": 15.0, "individual_sessions": []},
            },
        ],
    }))
    (state_dir / "01_sessions.json").write_text(json.dumps({"sessions": []}))
    slot = make_slot(
        location="Kwality House, Kemps Corner",
        class_name="Studio Barre 57",
        trainer_1="Trainer X",
        day_of_week="Monday",
        time="09:30",
        historical_session_count=0,
        historical_avg_checkin=0,
        historical_avg_fill=0,
    ).__dict__

    OutputReporter()._write_schedule_data(
        {"Kwality House, Kemps Corner": [slot]},
        "2026-05-04",
        {"trainer_metrics": []},
    )

    data = json.loads((web_dir / "schedule_data.json").read_text())
    enriched = data["locations"]["Kwality House, Kemps Corner"][0]
    assert enriched["metric_source"] == "nearby_class_day_time_location"
    assert enriched["metric_session_count"] == 3
    assert enriched["metric_avg_checkin"] == 6.0
    assert enriched["slot_avg_checkin"] is None
    assert enriched["slot_session_count"] is None
    assert enriched["slot_historic_detail"]["session_rows"] == 3
    assert enriched["slot_historic_detail"]["_fallback_source"] == "nearby_class_day_time_location"


def test_reporter_preserves_scheduled_score_when_metric_context_uses_fallback(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    web_dir = tmp_path / "web"
    state_dir.mkdir()
    web_dir.mkdir()
    monkeypatch.setattr(reporter_module, "STATE_DIR", state_dir)
    monkeypatch.setattr(reporter_module, "WEB_DIR", web_dir)
    scheduled_breakdown = {
        "total_score": 88.0,
        "formula": "scheduled optimizer score",
        "components": [
            {"key": "scheduled", "label": "Scheduled", "points": 88.0, "max_points": 100.0}
        ],
    }
    fallback_breakdown = {
        "total_score": 40.0,
        "formula": "fallback historical metric score",
        "components": [
            {"key": "fallback", "label": "Fallback", "points": 40.0, "max_points": 100.0}
        ],
    }
    (state_dir / "03_scores.json").write_text(json.dumps({
        "class_slot_ranking": [],
        "slot_group_ranking": [
            {
                "location": "Kwality House, Kemps Corner",
                "class": "Studio Barre 57",
                "day_name": "Monday",
                "time": "09:00",
                "session_count": 4,
                "avg_attendance": 8.0,
                "avg_checkin": 8.0,
                "avg_fill_rate": 0.40,
                "score": 40.0,
                "score_breakdown": fallback_breakdown,
                "historic_detail": {"session_rows": 4, "avg_checked_in": 8.0, "individual_sessions": []},
            }
        ],
    }))
    (state_dir / "01_sessions.json").write_text(json.dumps({"sessions": []}))
    slot = make_slot(
        score=88.0,
        performance_score=88.0,
        placement_score=91.0,
        score_breakdown=scheduled_breakdown,
        historical_session_count=0,
        historical_avg_checkin=0,
        historical_avg_fill=0,
    ).__dict__

    OutputReporter()._write_schedule_data(
        {"Kwality House, Kemps Corner": [slot]},
        "2026-05-04",
        {"trainer_metrics": []},
    )

    data = json.loads((web_dir / "schedule_data.json").read_text())
    enriched = data["locations"]["Kwality House, Kemps Corner"][0]
    assert enriched["score"] == 88.0
    assert enriched["optimizer_score"] == 88.0
    assert enriched["metric_score"] == 40.0
    assert enriched["score_breakdown"] == scheduled_breakdown
    assert enriched["metric_score_breakdown"] == fallback_breakdown


def test_web_template_score_formula_matches_current_scorer_contract():
    template = web_ui_source()

    assert "normalized average attendance × 45" not in template
    assert "45% weight" not in template
    assert "average attendance × 75" in template
    assert "fill × 15" in template
    assert "revenue × 7" in template
    assert "sessions × 3" in template
    assert "PowerCycle: fill × 50" in template
    assert "Strength: fill × 70" in template


def test_class_cards_use_slot_metric_fill_fallback():
    template = web_ui_source()

    mini_start = template.index("function makeClassCardMini")
    mini_end = template.index("function makeClassCard", mini_start + 1)
    full_start = mini_end
    full_end = template.index("// ============================================================", full_start)
    mini_source = template[mini_start:mini_end]
    full_source = template[full_start:full_end]

    assert "const fill=classSlotFill(s);" in mini_source
    assert "const fill=classSlotFill(s);" in full_source
    assert "const fill=s.predicted_fill_rate||0;" not in mini_source
    assert "const fill=s.predicted_fill_rate||0;" not in full_source
    assert "const modalFill=drillMetrics(s).fill;" in template


def test_class_cards_show_ai_optimization_icon():
    template = web_ui_source()

    mini_start = template.index("function makeClassCardMini")
    mini_end = template.index("function makeClassCard", mini_start + 1)
    full_start = mini_end
    full_end = template.index("// ============================================================", full_start)
    mini_source = template[mini_start:mini_end]
    full_source = template[full_start:full_end]

    assert "function hasAiOptimizationChange" in template
    assert "function aiOptimizedIcon" in template
    assert "s?.ai_optimized" in template
    assert "s?.ai_added" in template
    assert "s?.ai_moved" in template
    assert "${aiOptimizedIcon(s)}" in mini_source
    assert "${aiOptimizedIcon(s)}" in full_source


def test_drilldown_session_table_has_required_columns_and_totals_row():
    template = web_ui_source()
    start = template.index("function sessionRowsTable")
    end = template.index("function historicDrilldownHtml", start)
    source = template[start:end]

    required_headers = [
        "Class Name",
        "Day of Week",
        "Time",
        "Date",
        "Trainer Name",
        "Location",
        "Capacity",
        "Booked",
        "Checked In",
        "Cancelled",
        "Late Cancelled",
        "Total Sessions",
        "Empty Sessions",
        "Class Avg - Incl Empty",
        "Class Avg - Excl Empty",
        "Fill Rate",
        "Revenue Generated",
        "Rev / Member",
    ]
    for header in required_headers:
        assert f"<th>{header}</th>" in source

    assert "<tfoot>" in source
    assert "Totals" in source
    assert "sessionRowsTotals" in source
    assert "revMember" in source
    assert "drillSessionTableHtml(rows)" in template


def test_web_template_labels_metric_source_in_class_cards():
    template = web_ui_source()

    assert "function metricSourceShortLabel" in template
    assert "canonical" in template
    assert "nearby" in template


def test_web_template_does_not_cap_trainer_workload_overview_to_16_trainers():
    template = web_ui_source()

    assert ".slice(0,16)" not in template


def test_web_template_uses_single_control_center_entry_in_main_tabs():
    template = web_ui_source()

    assert 'id="vbtn-control"' in template
    assert "Control Center" in template
    assert 'id="vbtn-history"' not in template
    assert 'id="vbtn-rules"' not in template
    assert 'id="vbtn-settings"' not in template


def test_web_template_control_center_uses_clear_section_labels():
    template = web_ui_source()

    assert "Schedule Setup" in template
    assert "Trainer Setup" in template
    assert "Class Mix and Formats" in template
    assert "Rules and Pinned Classes" in template
    assert "AI & Generation" in template
    assert "Rule Catalog" not in template


def test_settings_console_has_single_shell_and_generation_status():
    template = web_ui_source()

    assert "Settings Console" in template
    assert "Applied to every generation" in template
    assert "control-center-inspector" in template
    assert "sett-generation-contract" in template
    assert "settings-console-layout" in template
    assert "sett-command" not in template
    assert "sett-rail" not in template


def test_settings_console_consolidates_availability_rules_and_generation_options():
    template = web_ui_source()

    assert "Weekly Availability" in template
    assert "Leave & Off Days" in template
    assert "avail-location-card" in template
    assert "customrules-builder" in template
    assert "manualpins-builder" in template
    assert "universal-rules-list" in template
    for key in (
        "enforce_assignment_days",
        "enforce_leave_and_off_days",
        "trainer_week_off_strategy",
        "max_week_off_days",
        "protect_pinned_classes_from_repair",
        "hard_block_inactive_trainers",
        "require_certified_format_match",
        "tier1_min_weekly_hours",
        "tier1_ideal_weekly_hours",
        "max_daily_trainer_hours",
        "max_trainer_work_days",
        "deepseek_api_key",
        "deepseek_model",
        "deepseek_base_url",
        "ai_backup_model",
    ):
        assert key in template


def test_settings_console_defaults_tier1_weekly_hours_to_13_to_15():
    template = web_ui_source()

    assert "tier1_min_weekly_hours:13" in template
    assert "tier1_ideal_weekly_hours:15" in template


def test_settings_console_certifications_use_current_format_names():
    template = web_ui_source()

    assert "Studio Barre 57" in template
    assert "Studio PowerCycle Express" in template
    assert "Studio Back Body Blaze" in template
    assert "Barre/Cardio/Mat Express" in template


def test_web_template_uses_single_speed_calendar_logo():
    template = web_ui_source()

    assert template.count("images/plan57-speed-calendar-v2.png") == 2
    assert "images/plan57-speed-calendar.png" not in template
    assert "images/plan57-calendar-gold.png" not in template
    assert "images/plan57-calendar-red.png" not in template
    assert "images/plan57-dark-badge.png" not in template
    assert 'src="/images/plan57' not in template
    assert 'href="/images/plan57' not in template
    assert 'class="logo-img"' in template
    assert "brand-wordmark" in template
    assert 'class="chat-brand-mark"' in template
    assert 'class="chat-fab-logo"' in template


def test_web_template_uses_dedicated_ai_agent_logo_and_advanced_rule_builder():
    template = web_ui_source()

    assert template.count("images/plan57-ai-agent-v2.png") == 2
    assert "images/plan57-ai-agent.png" not in template
    assert 'class="chat-fab-logo" src="images/plan57-ai-agent-v2.png"' in template
    assert 'class="chat-brand-mark" src="images/plan57-ai-agent-v2.png"' in template
    assert "trainer_load_limit" in template
    assert "room_capacity_rule" in template
    assert "sequence_spacing_rule" in template
    assert "time_window_rule" in template
    assert "data-cr-field=\"time_end\"" in template
    assert "data-cr-field=\"room\"" in template
    assert "data-cr-field=\"condition\"" in template
    assert "advanced-rule-builder" in template


def test_class_cards_use_modern_sleek_card_styles():
    template = web_ui_source()

    assert "class-card-modern-surface" in template
    assert ".cc::after" in template
    assert "backdrop-filter:blur" in template
    assert "cubic-bezier(.16,1,.3,1)" in template
    assert "cc-card-kicker" in template
    assert "cc-metric-pill" in template
    assert "cc-tool-icon" in template
    assert "Advanced Planner Settings" in template


def test_rule_catalog_links_to_guided_custom_rule_builder():
    template = web_ui_source()

    assert "Create Custom Rule" in template
    assert "function rvOpenCustomRuleBuilder" in template
    assert "settSetTab(\"customrules\")" in template
    assert "CUSTOM_RULE_TEMPLATES" in template
    assert "function settUpdateCustomRuleBuilder" in template
    assert "data-cr-field" in template
    assert "Rule preview" in template


def test_pipeline_request_normalizes_selected_date_and_can_force_standard_generation(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(flask_app_module, "SCHEDULE_CONFIG_PATH", config_dir / "schedule_config.json")

    options = flask_app_module._resolve_pipeline_request_options({
        "week_start": "2026-05-06",
        "use_ai": False,
    })

    assert options["week"] == "2026-05-04"
    assert options["use_ai"] is False
    assert options["child_env"]["SCHEDULER_FORCE_GREEDY"] == "1"


def test_pipeline_request_uses_saved_ai_key_when_ai_generation_requested(tmp_path, monkeypatch):
    clear_ai_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "schedule_config.json"
    config_path.write_text(json.dumps({
        "settings_options": {
            "ai_api_key": "saved-test-key",
        }
    }))
    monkeypatch.setattr(flask_app_module, "SCHEDULE_CONFIG_PATH", config_path)

    options = flask_app_module._resolve_pipeline_request_options({
        "week_start": "2026-05-11",
        "use_ai": True,
    })

    assert options["week"] == "2026-05-11"
    assert options["use_ai"] is True
    assert options["child_env"]["OPENROUTER_API_KEY"] == "saved-test-key"
    assert options["child_env"]["SCHEDULER_FORCE_AI_ONLY"] == "1"
    assert "SCHEDULER_FORCE_GREEDY" not in options["child_env"]


def test_pipeline_request_prefers_env_ai_key_over_saved_control_center_key(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "schedule_config.json"
    config_path.write_text(json.dumps({
        "settings_options": {
            "ai_api_key": "saved-test-key",
        }
    }))
    monkeypatch.setattr(flask_app_module, "SCHEDULE_CONFIG_PATH", config_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-openrouter-key")
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")

    options = flask_app_module._resolve_pipeline_request_options({
        "week_start": "2026-05-11",
        "use_ai": True,
    })

    assert options["child_env"]["OPENROUTER_API_KEY"] == "env-openrouter-key"


def test_pipeline_request_prefers_env_ai_key_over_payload_control_center_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-openrouter-key")

    options = flask_app_module._resolve_pipeline_request_options({
        "week_start": "2026-05-11",
        "use_ai": True,
        "api_key": "payload-openrouter-key",
    })

    assert options["child_env"]["OPENROUTER_API_KEY"] == "env-openrouter-key"


def test_serve_pipeline_request_uses_saved_ai_key_when_ai_generation_requested(tmp_path, monkeypatch):
    clear_ai_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "schedule_config.json"
    config_path.write_text(json.dumps({
        "settings_options": {
            "ai_api_key": "saved-test-key",
        }
    }))
    monkeypatch.setattr(serve_module, "SCHEDULE_CONFIG_PATH", config_path)

    options = serve_module._resolve_pipeline_request_options({
        "week_start": "2026-05-11",
        "use_ai": True,
    }, "2026-05-04")

    assert options["week"] == "2026-05-11"
    assert options["use_ai"] is True
    assert options["child_env"]["OPENROUTER_API_KEY"] == "saved-test-key"
    assert options["child_env"]["SCHEDULER_FORCE_AI_ONLY"] == "1"
    assert "SCHEDULER_FORCE_GREEDY" not in options["child_env"]


def test_serve_pipeline_request_prefers_env_ai_key_over_saved_control_center_key(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "schedule_config.json"
    config_path.write_text(json.dumps({
        "settings_options": {
            "ai_api_key": "saved-test-key",
        }
    }))
    monkeypatch.setattr(serve_module, "SCHEDULE_CONFIG_PATH", config_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-openrouter-key")
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")

    options = serve_module._resolve_pipeline_request_options({
        "week_start": "2026-05-11",
        "use_ai": True,
    }, "2026-05-04")

    assert options["child_env"]["OPENROUTER_API_KEY"] == "env-openrouter-key"


def test_serve_pipeline_request_prefers_env_ai_key_over_payload_control_center_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-openrouter-key")

    options = serve_module._resolve_pipeline_request_options({
        "week_start": "2026-05-11",
        "use_ai": True,
        "api_key": "payload-openrouter-key",
    }, "2026-05-04")

    assert options["child_env"]["OPENROUTER_API_KEY"] == "env-openrouter-key"


def test_pipeline_request_uses_deepseek_primary_and_openrouter_fallback(tmp_path, monkeypatch):
    clear_ai_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "schedule_config.json"
    config_path.write_text(json.dumps({
        "settings_options": {
            "deepseek_api_key": "saved-deepseek-key",
            "deepseek_model": "deepseek-v4-flash",
            "ai_provider": "deepseek",
            "ai_api_key": "saved-openrouter-key",
            "ai_model": "openai/gpt-oss-120b:free",
            "ai_backup_model": "z-ai/glm-4.5-air:free",
        }
    }))
    monkeypatch.setattr(flask_app_module, "SCHEDULE_CONFIG_PATH", config_path)

    options = flask_app_module._resolve_pipeline_request_options({
        "week_start": "2026-05-11",
        "use_ai": True,
    })

    assert options["child_env"]["DEEPSEEK_API_KEY"] == "saved-deepseek-key"
    assert options["child_env"]["DEEPSEEK_MODEL"] == "deepseek-v4-flash"
    assert options["child_env"]["DEEPSEEK_BASE_URL"] == "https://api.deepseek.com"
    assert options["child_env"]["OPENROUTER_API_KEY"] == "saved-openrouter-key"
    assert options["child_env"]["OPENROUTER_MODEL"] == "openai/gpt-oss-120b:free"
    assert options["child_env"]["OPENROUTER_BACKUP_MODEL"] == "z-ai/glm-4.5-air:free"
    assert options["child_env"]["SCHEDULER_FORCE_AI_ONLY"] == "1"


def test_pipeline_request_prefers_env_deepseek_and_openrouter_over_saved_keys(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "schedule_config.json"
    config_path.write_text(json.dumps({
        "settings_options": {
            "deepseek_api_key": "saved-deepseek-key",
            "ai_provider": "deepseek",
            "ai_api_key": "saved-openrouter-key",
        }
    }))
    monkeypatch.setattr(flask_app_module, "SCHEDULE_CONFIG_PATH", config_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-deepseek-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-openrouter-key")

    options = flask_app_module._resolve_pipeline_request_options({
        "week_start": "2026-05-11",
        "use_ai": True,
    })

    assert options["child_env"]["DEEPSEEK_API_KEY"] == "env-deepseek-key"
    assert options["child_env"]["OPENROUTER_API_KEY"] == "env-openrouter-key"


def test_serve_pipeline_request_uses_deepseek_primary_and_openrouter_fallback(tmp_path, monkeypatch):
    clear_ai_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "schedule_config.json"
    config_path.write_text(json.dumps({
        "settings_options": {
            "deepseek_api_key": "saved-deepseek-key",
            "deepseek_model": "deepseek-v4-flash",
            "ai_provider": "deepseek",
            "ai_api_key": "saved-openrouter-key",
            "ai_model": "openai/gpt-oss-120b:free",
            "ai_backup_model": "z-ai/glm-4.5-air:free",
        }
    }))
    monkeypatch.setattr(serve_module, "SCHEDULE_CONFIG_PATH", config_path)

    options = serve_module._resolve_pipeline_request_options({
        "week_start": "2026-05-11",
        "use_ai": True,
    }, "2026-05-04")

    assert options["child_env"]["DEEPSEEK_API_KEY"] == "saved-deepseek-key"
    assert options["child_env"]["DEEPSEEK_MODEL"] == "deepseek-v4-flash"
    assert options["child_env"]["DEEPSEEK_BASE_URL"] == "https://api.deepseek.com"
    assert options["child_env"]["OPENROUTER_API_KEY"] == "saved-openrouter-key"
    assert options["child_env"]["OPENROUTER_MODEL"] == "openai/gpt-oss-120b:free"
    assert options["child_env"]["OPENROUTER_BACKUP_MODEL"] == "z-ai/glm-4.5-air:free"
    assert options["child_env"]["SCHEDULER_FORCE_AI_ONLY"] == "1"


def test_serve_pipeline_request_prefers_env_deepseek_and_openrouter_over_saved_keys(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "schedule_config.json"
    config_path.write_text(json.dumps({
        "settings_options": {
            "deepseek_api_key": "saved-deepseek-key",
            "ai_provider": "deepseek",
            "ai_api_key": "saved-openrouter-key",
        }
    }))
    monkeypatch.setattr(serve_module, "SCHEDULE_CONFIG_PATH", config_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-deepseek-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-openrouter-key")

    options = serve_module._resolve_pipeline_request_options({
        "week_start": "2026-05-11",
        "use_ai": True,
    }, "2026-05-04")

    assert options["child_env"]["DEEPSEEK_API_KEY"] == "env-deepseek-key"
    assert options["child_env"]["OPENROUTER_API_KEY"] == "env-openrouter-key"


def test_serve_optimize_schedule_endpoint_is_registered(monkeypatch):
    from http.server import HTTPServer
    import threading
    from urllib import request as urlrequest

    monkeypatch.setattr(
        serve_module,
        "_optimize_schedule_request",
        lambda payload: {"ok": True, "summary": "test patch", "applied_count": 0, "rejected_count": 0},
        raising=False,
    )
    server = HTTPServer(("127.0.0.1", 0), serve_module.RulesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urlrequest.Request(
            f"http://127.0.0.1:{server.server_port}/api/optimize-schedule",
            data=b'{"iteration":"Main"}',
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlrequest.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
        assert resp.status == 200
        assert body["ok"] is True
        assert body["summary"] == "test patch"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_serve_optimize_schedule_defaults_to_deepseek_key(tmp_path, monkeypatch):
    clear_ai_env(monkeypatch)
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "schedule_data.json").write_text(json.dumps({
        "locations": {
            "Supreme HQ, Bandra": [{
                "location": "Supreme HQ, Bandra",
                "day_of_week": "Monday",
                "time": "09:00",
                "class_name": "Studio Barre 57",
                "trainer_1": "Trainer A",
                "score": 20,
                "predicted_fill_rate": 0.2,
            }]
        }
    }))
    config_path = tmp_path / "schedule_config.json"
    config_path.write_text(json.dumps({
        "settings_options": {"deepseek_api_key": "deepseek-key"}
    }))
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": '{"summary":"No changes","operations":[]}'}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json
            return FakeResponse()

    class FakeTimeout:
        def __init__(self, *args, **kwargs):
            pass

    class FakeHttpx:
        Client = FakeClient
        Timeout = FakeTimeout
        TimeoutException = TimeoutError

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)
    monkeypatch.setattr(serve_module, "WEB_DIR", web_dir)
    monkeypatch.setattr(serve_module, "SCHEDULE_CONFIG_PATH", config_path)

    result = serve_module._run_optimize_with_ai({"location": "Supreme HQ, Bandra"})

    assert result["ok"] is True
    assert result["model"] == "deepseek-v4-flash"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer deepseek-key"
    assert captured["body"]["thinking"] == {"type": "disabled"}


def test_serve_optimize_schedule_prefers_env_key_over_control_center_key(tmp_path, monkeypatch):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "schedule_data.json").write_text(json.dumps({
        "locations": {
            "Supreme HQ, Bandra": [{
                "location": "Supreme HQ, Bandra",
                "day_of_week": "Monday",
                "time": "09:00",
                "class_name": "Studio Barre 57",
                "trainer_1": "Trainer A",
                "score": 20,
                "predicted_fill_rate": 0.2,
            }]
        }
    }))
    config_path = tmp_path / "schedule_config.json"
    config_path.write_text(json.dumps({
        "settings_options": {
            "ai_optimize_api_key": "saved-optimize-key",
            "ai_optimize_model": "gpt-4o-mini",
            "ai_optimize_base_url": "https://api.openai.com/v1",
            "deepseek_api_key": "saved-deepseek-key",
        }
    }))
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": '{"summary":"No changes","operations":[]}'}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["body"] = json
            captured["headers"] = headers
            return FakeResponse()

    class FakeTimeout:
        def __init__(self, *args, **kwargs):
            pass

    class FakeHttpx:
        Client = FakeClient
        Timeout = FakeTimeout
        TimeoutException = TimeoutError

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)
    monkeypatch.setattr(serve_module, "WEB_DIR", web_dir)
    monkeypatch.setattr(serve_module, "SCHEDULE_CONFIG_PATH", config_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-deepseek-key")

    result = serve_module._run_optimize_with_ai({"location": "Supreme HQ, Bandra"})

    assert result["ok"] is True
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer env-deepseek-key"
    assert captured["body"]["model"] == "deepseek-v4-flash"


def test_serve_accepts_british_optimise_schedule_alias(monkeypatch):
    from http.server import HTTPServer
    import threading
    from urllib import request as urlrequest

    monkeypatch.setattr(
        serve_module,
        "_optimize_schedule_request",
        lambda payload: {"ok": True, "summary": "alias patch", "applied_count": 0, "rejected_count": 0},
        raising=False,
    )
    server = HTTPServer(("127.0.0.1", 0), serve_module.RulesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urlrequest.Request(
            f"http://127.0.0.1:{server.server_port}/api/optimise-schedule",
            data=b'{"iteration":"Main"}',
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlrequest.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
        assert resp.status == 200
        assert body["ok"] is True
        assert body["summary"] == "alias patch"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_serve_optimize_schedule_accepts_numeric_ai_slot_ids(tmp_path, monkeypatch):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    schedule_path = web_dir / "schedule_data.json"
    schedule_path.write_text(json.dumps({
        "locations": {
            "Supreme HQ, Bandra": [
                {
                    "location": "Supreme HQ, Bandra",
                    "day_of_week": "Monday",
                    "time": "09:00",
                    "class_name": "Studio Barre 57",
                    "trainer_1": "Trainer A",
                    "score": 20,
                    "predicted_fill_rate": 0.2,
                }
            ]
        }
    }))
    config_path = tmp_path / "schedule_config.json"
    config_path.write_text(json.dumps({
        "settings_options": {"ai_optimize_api_key": "test-key"}
    }))
    profiles_path = tmp_path / "trainer_profiles.json"
    profiles_path.write_text(json.dumps([
        {"name": "Trainer A", "tier": 1, "qualifications": {"barre_57": True}, "locations": {"Supreme HQ, Bandra": {"available_days": ["Monday"], "time_window": {"start": "06:00", "end": "22:00"}, "max_classes_per_day": 4}}},
        {"name": "Trainer B", "tier": 1, "qualifications": {"barre_57": True}, "locations": {"Supreme HQ, Bandra": {"available_days": ["Monday"], "time_window": {"start": "06:00", "end": "22:00"}, "max_classes_per_day": 4}}},
    ]))

    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "operations": [{
                                "type": "swap_trainer",
                                "id": "1",
                                "new_trainer": "Trainer B",
                                "reason": "Better fit.",
                            }]
                        })
                    }
                }]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            captured["body"] = json
            return FakeResponse()

    class FakeTimeout:
        def __init__(self, *args, **kwargs):
            pass

    class FakeHttpx:
        Client = FakeClient
        Timeout = FakeTimeout
        TimeoutException = TimeoutError

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)
    monkeypatch.setattr(serve_module, "WEB_DIR", web_dir)
    monkeypatch.setattr(serve_module, "SCHEDULE_CONFIG_PATH", config_path)
    monkeypatch.setattr(serve_module, "TRAINER_PROFILES_PATH", profiles_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(serve_module, "STATE_DIR", state_dir)
    monkeypatch.setattr(serve_module, "OUTPUTS_DIR", tmp_path / "outputs")

    result = serve_module._run_optimize_with_ai({"location": "Supreme HQ, Bandra"})

    assert result["ok"] is True
    assert result["applied_count"] == 1
    assert result["rejected_count"] == 0
    assert json.loads(schedule_path.read_text())["locations"]["Supreme HQ, Bandra"][0]["trainer_1"] == "Trainer B"
    prompt_text = captured["body"]["messages"][1]["content"]
    assert '"id": "1"' in prompt_text


def test_serve_optimize_schedule_applies_mixed_validated_operations(tmp_path, monkeypatch):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    schedule_path = web_dir / "schedule_data.json"
    schedule_path.write_text(json.dumps({
        "locations": {
            "Supreme HQ, Bandra": [
                {
                    "location": "Supreme HQ, Bandra",
                    "date": "2026-05-11",
                    "day_of_week": "Monday",
                    "time": "09:00",
                    "class_name": "Studio Barre 57",
                    "trainer_1": "Trainer A",
                    "room": "studio_a",
                    "duration_min": 57,
                    "score": 20,
                    "predicted_fill_rate": 0.2,
                },
                {
                    "location": "Supreme HQ, Bandra",
                    "date": "2026-05-11",
                    "day_of_week": "Tuesday",
                    "time": "11:00",
                    "class_name": "Studio Barre 57",
                    "trainer_1": "Trainer A",
                    "room": "studio_a",
                    "duration_min": 57,
                    "score": 30,
                    "predicted_fill_rate": 0.3,
                },
            ]
        }
    }))
    config_path = tmp_path / "schedule_config.json"
    config_path.write_text(json.dumps({
        "settings_options": {"ai_optimize_api_key": "test-key"},
        "custom_rules": [{"name": "Supreme prime time", "description": "Keep strong formats before 10:00"}],
    }))
    profiles_path = tmp_path / "trainer_profiles.json"
    profiles_path.write_text(json.dumps([
        {
            "name": "Trainer A",
            "tier": 1,
            "qualifications": {"barre_57": True, "cardio_barre": True},
            "locations": {"Supreme HQ, Bandra": {"available_days": ["Monday", "Tuesday"], "time_window": {"start": "06:00", "end": "22:00"}, "max_classes_per_day": 4}},
        },
        {
            "name": "Trainer B",
            "tier": 1,
            "qualifications": {"barre_57": True, "cardio_barre": True},
            "locations": {"Supreme HQ, Bandra": {"available_days": ["Monday", "Tuesday"], "time_window": {"start": "06:00", "end": "22:00"}, "max_classes_per_day": 4}},
        },
    ]))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "03_scores.json").write_text(json.dumps({
        "class_slot_ranking": [
                {
                    "location": "Supreme HQ, Bandra",
                    "day_name": "Monday",
                    "time": "09:00",
                "class": "Studio Cardio Barre",
                "trainer": "Trainer B",
                "score": 91,
                "avg_fill_rate": 0.82,
                    "avg_checkin": 14.0,
                    "session_count": 8,
                },
                {
                    "location": "Supreme HQ, Bandra",
                    "day_name": "Tuesday",
                    "time": "12:30",
                    "class": "Studio Barre 57",
                    "trainer": "Trainer A",
                    "score": 88,
                    "avg_fill_rate": 0.7,
                    "avg_checkin": 12.0,
                    "session_count": 6,
                }
            ]
        }))

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "summary": "Broadened optimisation using rules and history.",
                            "operations": [
                                {"type": "swap_trainer", "id": "1", "new_trainer": "Trainer B", "reason": "Trainer B has stronger Monday 09:00 history."},
                                {"type": "change_class", "id": "1", "new_class": "Studio Cardio Barre", "reason": "Cardio Barre has stronger prime slot performance."},
                                {"type": "move_class", "id": "2", "target": {"location": "Supreme HQ, Bandra", "day_of_week": "Tuesday", "time": "12:30"}, "reason": "Move low-performing class out of crowded late morning."},
                                {"type": "add_class", "slot": {"location": "Supreme HQ, Bandra", "date": "2026-05-11", "day_of_week": "Monday", "time": "17:00", "class_name": "Studio Barre 57", "trainer_1": "Trainer A", "room": "studio_b", "duration_min": 57}, "reason": "Adds evening coverage with qualified trainer."},
                                {"type": "remove_class", "id": "99", "allow_delete": True, "reason": "Unknown class removal should be skipped."},
                            ],
                        })
                    }
                }]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            return FakeResponse()

    class FakeTimeout:
        def __init__(self, *args, **kwargs):
            pass

    class FakeHttpx:
        Client = FakeClient
        Timeout = FakeTimeout
        TimeoutException = TimeoutError

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)
    monkeypatch.setattr(serve_module, "WEB_DIR", web_dir)
    monkeypatch.setattr(serve_module, "SCHEDULE_CONFIG_PATH", config_path)
    monkeypatch.setattr(serve_module, "TRAINER_PROFILES_PATH", profiles_path)
    monkeypatch.setattr(serve_module, "STATE_DIR", state_dir)
    monkeypatch.setattr(serve_module, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(serve_module, "_regenerate_index_from_template", lambda data=None: None)
    monkeypatch.setattr(serve_module, "_save_schedule_to_supabase", lambda data: {"saved": False})

    result = serve_module._run_optimize_with_ai({"location": "Supreme HQ, Bandra"})

    assert result["ok"] is True
    assert result["applied_count"] == 4
    assert result["rejected_count"] == 1
    assert {item["type"] for item in result["applied"]} == {"swap_trainer", "change_class", "move_class", "add_class"}
    assert all(item.get("reason") for item in result["applied"])
    assert all(item.get("validation") for item in result["applied"])
    assert any("history" in " ".join(item.get("evidence", [])) for item in result["applied"])
    rows = json.loads(schedule_path.read_text())["locations"]["Supreme HQ, Bandra"]
    assert any(r["class_name"] == "Studio Cardio Barre" and r["trainer_1"] == "Trainer B" for r in rows)
    assert any(r["time"] == "12:30" and r.get("ai_optimized") for r in rows)
    assert any(r["time"] == "17:00" and r.get("ai_added") for r in rows)


def test_optimizer_history_evidence_does_not_cite_unrelated_rows():
    evidence = serve_module._optimizer_history_evidence(
        {
            "location": "Kwality House, Kemps Corner",
            "day_of_week": "Monday",
            "time": "11:00",
            "class_name": "Studio Cardio Barre",
            "trainer_1": "Reshma Sharma",
            "predicted_fill_rate": 0.1,
            "score": 12,
        },
        {
            "class_slot_ranking": [{
                "location": "Kwality House, Kemps Corner",
                "day": "Friday",
                "time": "11:00",
                "class": "Studio Cardio Barre",
                "trainer": "Cauveri Vikrant",
                "score": 100,
                "fill": 0.82,
                "checkins": 18,
                "sessions": 1,
            }]
        },
    )

    assert len(evidence) == 1
    assert "No class/trainer or class-slot historical evidence" in evidence[0]
    assert "Cauveri Vikrant" not in evidence[0]
    assert "82%" not in evidence[0]


def test_optimizer_report_uses_server_generated_reason_for_remove_and_top_level_add():
    schedule_data = {
        "locations": {
            "Kwality House, Kemps Corner": [{
                "location": "Kwality House, Kemps Corner",
                "day_of_week": "Monday",
                "time": "11:00",
                "class_name": "Studio Cardio Barre",
                "trainer_1": "Reshma Sharma",
                "room": "studio_a",
                "duration_min": 57,
                "predicted_fill_rate": 0.1,
                "score": 12,
            }]
        }
    }
    compact = [{
        "id": "1",
        "loc": "Kwality House, Kemps Corner",
        "schedule_index": 0,
        "slot": schedule_data["locations"]["Kwality House, Kemps Corner"][0],
    }]

    applied, rejected = serve_module._apply_ai_schedule_operations(
        schedule_data,
        [
            {"type": "remove_class", "id": "1", "allow_delete": True, "reason": "bad historical performance"},
            {
                "type": "add_class",
                "location": "Kwality House, Kemps Corner",
                "day_of_week": "Monday",
                "time": "12:30",
                "class_name": "Studio Barre 57",
                "trainer_1": "Reshma Sharma",
                "room": "studio_a",
                "duration_min": 57,
                "reason": "replace with better class",
            },
        ],
        compact,
        {"Reshma Sharma"},
        "Main",
        {"class_slot_ranking": [{
            "location": "Kwality House, Kemps Corner",
            "day": "Monday",
            "time": "11:00",
            "class": "Studio Cardio Barre",
            "trainer": "Reshma Sharma",
            "fill": 0.20,
            "checkins": 3,
            "sessions": 8,
        }]},
        {"custom_rules": []},
    )

    assert rejected == []
    assert applied[0]["type"] == "remove_class"
    assert "current slot metrics are weak" in applied[0]["reason"]
    assert "AI note:" not in applied[0]["reason"]
    assert applied[1]["type"] == "add_class"
    assert applied[1]["after"]["time"] == "12:30"
    assert "Added Studio Barre 57" in applied[1]["reason"]


def test_optimizer_rejects_delete_without_explicit_allow_delete():
    slot = {
        "location": "Kwality House, Kemps Corner",
        "day_of_week": "Monday",
        "time": "19:15",
        "class_name": "Studio Trainer's Choice",
        "trainer_1": "Simonelle De Vitre",
        "predicted_fill_rate": 0.28,
        "score": 65,
    }
    schedule_data = {"locations": {"Kwality House, Kemps Corner": [slot]}}
    compact = [{"id": "1", "loc": "Kwality House, Kemps Corner", "schedule_index": 0, "slot": slot}]

    applied, rejected = serve_module._apply_ai_schedule_operations(
        schedule_data,
        [{"type": "remove_class", "id": "1", "reason": "low fill"}],
        compact,
        {"Simonelle De Vitre"},
        "Main",
        {"class_slot_ranking": []},
        {},
    )

    assert applied == []
    assert rejected[0]["message"].startswith("Deletion not applied")
    assert schedule_data["locations"]["Kwality House, Kemps Corner"] == [slot]


def test_optimizer_rejects_delete_without_bad_history_or_room_conflict():
    slot = {
        "location": "Kwality House, Kemps Corner",
        "day_of_week": "Monday",
        "time": "19:15",
        "class_name": "Studio Trainer's Choice",
        "trainer_1": "Simonelle De Vitre",
        "room": "studio_a",
        "predicted_fill_rate": 0.28,
        "score": 65,
    }
    schedule_data = {"locations": {"Kwality House, Kemps Corner": [slot]}}
    compact = [{"id": "1", "loc": "Kwality House, Kemps Corner", "schedule_index": 0, "slot": slot}]

    applied, rejected = serve_module._apply_ai_schedule_operations(
        schedule_data,
        [{"type": "remove_class", "id": "1", "allow_delete": True, "reason": "low fill"}],
        compact,
        {"Simonelle De Vitre"},
        "Main",
        {"class_slot_ranking": [{
            "location": "Kwality House, Kemps Corner",
            "day": "Monday",
            "time": "19:15",
            "class": "Studio Trainer's Choice",
            "trainer": "Simonelle De Vitre",
            "fill": 0.6,
            "checkins": 12,
            "sessions": 12,
        }]},
        {},
    )

    assert applied == []
    assert rejected[0]["message"].startswith("Deletion not applied")
    assert "weak over time" in rejected[0]["message"]


def test_optimizer_allows_delete_for_bad_history_over_time():
    slot = {
        "location": "Kwality House, Kemps Corner",
        "day_of_week": "Monday",
        "time": "19:15",
        "class_name": "Studio Trainer's Choice",
        "trainer_1": "Simonelle De Vitre",
        "room": "studio_a",
        "predicted_fill_rate": 0.28,
        "score": 65,
    }
    schedule_data = {"locations": {"Kwality House, Kemps Corner": [slot]}}
    compact = [{"id": "1", "loc": "Kwality House, Kemps Corner", "schedule_index": 0, "slot": slot}]

    applied, rejected = serve_module._apply_ai_schedule_operations(
        schedule_data,
        [{"type": "remove_class", "id": "1", "allow_delete": True, "reason": "low fill"}],
        compact,
        {"Simonelle De Vitre"},
        "Main",
        {"class_slot_ranking": [{
            "location": "Kwality House, Kemps Corner",
            "day": "Monday",
            "time": "19:15",
            "class": "Studio Trainer's Choice",
            "trainer": "Simonelle De Vitre",
            "fill": 0.24,
            "checkins": 3,
            "sessions": 8,
        }]},
        {},
    )

    assert rejected == []
    assert applied[0]["type"] == "remove_class"
    assert schedule_data["locations"]["Kwality House, Kemps Corner"] == []


def test_optimizer_rejects_add_that_breaks_two_days_off_or_same_day_location_rule(monkeypatch):
    monkeypatch.setattr(serve_module, "_validate_manual_slot", lambda *args, **kwargs: None)
    existing = [
        {"location": "Kwality House, Kemps Corner", "day_of_week": day, "time": "09:00", "class_name": "Studio Barre 57", "trainer_1": "Trainer A", "duration_min": 57}
        for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    ]
    schedule_data = {"locations": {"Kwality House, Kemps Corner": existing, "Supreme HQ, Bandra": []}}

    applied, rejected = serve_module._apply_ai_schedule_operations(
        schedule_data,
        [{"type": "add_class", "location": "Supreme HQ, Bandra", "day_of_week": "Saturday", "time": "09:00", "class_name": "Studio Barre 57", "trainer_1": "Trainer A"}],
        [],
        {"Trainer A"},
        "Main",
        {"class_slot_ranking": []},
        {},
    )

    assert applied == []
    assert "at least 2 days off" in rejected[0]["message"]

    applied, rejected = serve_module._apply_ai_schedule_operations(
        schedule_data,
        [{"type": "add_class", "location": "Supreme HQ, Bandra", "day_of_week": "Monday", "time": "11:30", "class_name": "Studio Barre 57", "trainer_1": "Trainer A"}],
        [],
        {"Trainer A"},
        "Main",
        {"class_slot_ranking": []},
        {},
    )

    assert applied == []
    assert "more than one studio on Monday" in rejected[0]["message"]


def test_optimizer_rejects_change_class_without_exact_better_history(monkeypatch):
    monkeypatch.setattr(serve_module, "_validate_manual_slot", lambda *args, **kwargs: None)
    slot = {
        "location": "Kwality House, Kemps Corner",
        "day_of_week": "Monday",
        "time": "11:00",
        "class_name": "Studio Cardio Barre Express",
        "trainer_1": "Trainer A",
        "predicted_fill_rate": 0.10,
        "score": 35,
    }
    schedule_data = {"locations": {"Kwality House, Kemps Corner": [slot]}}
    compact = [{"id": "1", "loc": "Kwality House, Kemps Corner", "schedule_index": 0, "slot": slot}]

    applied, rejected = serve_module._apply_ai_schedule_operations(
        schedule_data,
        [{"type": "change_class", "id": "1", "new_class": "Studio Cardio Barre", "reason": "better format"}],
        compact,
        {"Trainer A"},
        "Main",
        {"class_slot_ranking": []},
        {},
    )

    assert applied == []
    assert rejected[0]["message"].startswith("Change not applied")


def test_optimizer_allows_change_class_with_slot_level_better_history(monkeypatch):
    monkeypatch.setattr(serve_module, "_validate_manual_slot", lambda *args, **kwargs: None)
    slot = {
        "location": "Kwality House, Kemps Corner",
        "day_of_week": "Monday",
        "time": "11:00",
        "class_name": "Studio Cardio Barre Express",
        "trainer_1": "Anisha Shah",
        "predicted_fill_rate": 0.10,
        "score": 35,
    }
    schedule_data = {"locations": {"Kwality House, Kemps Corner": [slot]}}
    compact = [{"id": "1", "loc": "Kwality House, Kemps Corner", "schedule_index": 0, "slot": slot}]

    applied, rejected = serve_module._apply_ai_schedule_operations(
        schedule_data,
        [{"type": "change_class", "id": "1", "new_class": "Studio Cardio Barre", "reason": "better format"}],
        compact,
        {"Anisha Shah"},
        "Main",
        {"class_slot_ranking": [{
            "location": "Kwality House, Kemps Corner",
            "day": "Monday",
            "time": "11:00",
            "class": "Studio Cardio Barre",
            "trainer": "Cauveri Vikrant",
            "fill": 0.82,
            "checkins": 14,
            "sessions": 8,
            "score": 100,
        }]},
        {},
    )

    assert rejected == []
    assert applied[0]["after"]["class_name"] == "Studio Cardio Barre"
    assert applied[0]["after"]["trainer_1"] == "Anisha Shah"


def test_optimizer_change_class_can_validate_combined_trainer_change(monkeypatch):
    monkeypatch.setattr(serve_module, "_validate_manual_slot", lambda *args, **kwargs: None)
    slot = {
        "location": "Kwality House, Kemps Corner",
        "day_of_week": "Monday",
        "time": "11:00",
        "class_name": "Studio Cardio Barre Express",
        "trainer_1": "Anisha Shah",
        "predicted_fill_rate": 0.10,
        "score": 35,
    }
    schedule_data = {"locations": {"Kwality House, Kemps Corner": [slot]}}
    compact = [{"id": "1", "loc": "Kwality House, Kemps Corner", "schedule_index": 0, "slot": slot}]

    applied, rejected = serve_module._apply_ai_schedule_operations(
        schedule_data,
        [{"type": "change_class", "id": "1", "new_class": "Studio Cardio Barre", "new_trainer": "Cauveri Vikrant", "reason": "better format and trainer"}],
        compact,
        {"Anisha Shah", "Cauveri Vikrant"},
        "Main",
        {"class_slot_ranking": [{
            "location": "Kwality House, Kemps Corner",
            "day": "Monday",
            "time": "11:00",
            "class": "Studio Cardio Barre",
            "trainer": "Cauveri Vikrant",
            "fill": 0.82,
            "checkins": 14,
            "sessions": 8,
            "score": 100,
        }]},
        {},
    )

    assert rejected == []
    assert applied[0]["after"]["class_name"] == "Studio Cardio Barre"
    assert applied[0]["after"]["trainer_1"] == "Cauveri Vikrant"


def test_optimizer_builds_server_vetted_candidate_operations(monkeypatch):
    monkeypatch.setattr(serve_module, "_validate_manual_slot", lambda *args, **kwargs: None)
    slot = {
        "location": "Kwality House, Kemps Corner",
        "day_of_week": "Monday",
        "time": "11:00",
        "class_name": "Studio Cardio Barre Express",
        "trainer_1": "Anisha Shah",
        "predicted_fill_rate": 0.10,
        "score": 35,
    }
    schedule_data = {"locations": {"Kwality House, Kemps Corner": [slot]}}
    compact = [{
        "id": "1",
        "loc": "Kwality House, Kemps Corner",
        "schedule_index": 0,
        "slot": slot,
        "fill": 0.10,
        "score": 35,
    }]

    candidates = serve_module._validated_optimizer_candidate_operations(
        schedule_data,
        compact,
        {"Anisha Shah", "Cauveri Vikrant"},
        "Main",
        {"class_slot_ranking": [
            {
                "location": "Kwality House, Kemps Corner",
                "day": "Monday",
                "time": "11:00",
                "class": "Studio Cardio Barre",
                "trainer": "Cauveri Vikrant",
                "fill": 0.82,
                "checkins": 14,
                "sessions": 8,
                "score": 100,
            },
            {
                "location": "Supreme HQ, Bandra",
                "day": "Monday",
                "time": "11:00",
                "class": "Studio FIT",
                "trainer": "Cauveri Vikrant",
                "fill": 0.9,
                "checkins": 16,
                "sessions": 8,
                "score": 100,
            },
        ]},
        {},
    )

    assert len(candidates) == 1
    assert candidates[0]["operation"] == {
        "type": "change_class",
        "id": "1",
        "new_class": "Studio Cardio Barre",
        "new_trainer": "Cauveri Vikrant",
        "reason": "Server-vetted class-slot improvement: Studio Cardio Barre with Cauveri Vikrant has score 100 and fill 82% over 8 sessions for this exact day/time/location.",
    }


def test_optimizer_ai_json_parser_handles_markdown_wrapped_json():
    parsed = serve_module._parse_optimizer_ai_json('```json\n{"summary":"ok","operations":[]}\n```')

    assert parsed["summary"] == "ok"
    assert parsed["operations"] == []


def test_optimizer_ai_json_parser_reports_truncated_response():
    with pytest.raises(ValueError, match="truncated"):
        serve_module._parse_optimizer_ai_json('{"summary":"ok","operations":[{"type":"change_class"}')


def test_serve_optimize_schedule_retries_when_all_ai_operations_rejected(tmp_path, monkeypatch):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    schedule_path = web_dir / "schedule_data.json"
    schedule_path.write_text(json.dumps({
        "locations": {
            "Supreme HQ, Bandra": [{
                "location": "Supreme HQ, Bandra",
                "day_of_week": "Monday",
                "time": "09:00",
                "class_name": "Studio Barre 57",
                "trainer_1": "Trainer A",
                "score": 20,
                "predicted_fill_rate": 0.2,
            }]
        }
    }))
    config_path = tmp_path / "schedule_config.json"
    config_path.write_text(json.dumps({"settings_options": {"ai_optimize_api_key": "test-key"}}))
    profiles_path = tmp_path / "trainer_profiles.json"
    profiles_path.write_text(json.dumps([
        {"name": "Trainer A", "tier": 1, "qualifications": {"barre_57": True}, "locations": {"Supreme HQ, Bandra": {"available_days": ["Monday"], "time_window": {"start": "06:00", "end": "22:00"}, "max_classes_per_day": 4}}},
        {"name": "Trainer B", "tier": 1, "qualifications": {"barre_57": True}, "locations": {"Supreme HQ, Bandra": {"available_days": ["Monday"], "time_window": {"start": "06:00", "end": "22:00"}, "max_classes_per_day": 4}}},
    ]))
    posts = []

    class FakeResponse:
        status_code = 200

        def __init__(self, content):
            self._content = content

        def json(self):
            return {"choices": [{"message": {"content": self._content}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            posts.append(json)
            if len(posts) == 1:
                return FakeResponse(json_module.dumps({
                    "operations": [{"type": "change_class", "id": "1", "new_class": "Studio Cardio Barre", "reason": "no exact evidence"}]
                }))
            return FakeResponse(json_module.dumps({
                "summary": "Retry found valid trainer swap.",
                "operations": [{"type": "swap_trainer", "id": "1", "new_trainer": "Trainer B", "reason": "valid profile and same slot"}],
            }))

    class FakeTimeout:
        def __init__(self, *args, **kwargs):
            pass

    class FakeHttpx:
        Client = FakeClient
        Timeout = FakeTimeout
        TimeoutException = TimeoutError

    import json as json_module
    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)
    monkeypatch.setattr(serve_module, "WEB_DIR", web_dir)
    monkeypatch.setattr(serve_module, "SCHEDULE_CONFIG_PATH", config_path)
    monkeypatch.setattr(serve_module, "TRAINER_PROFILES_PATH", profiles_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(serve_module, "STATE_DIR", state_dir)
    monkeypatch.setattr(serve_module, "OUTPUTS_DIR", tmp_path / "outputs")

    result = serve_module._run_optimize_with_ai({"location": "Supreme HQ, Bandra"})

    assert len(posts) == 2
    assert result["applied_count"] == 1
    assert result["summary"].startswith("Retry found")
    assert "Previous operations were all rejected" in posts[1]["messages"][-1]["content"]


def test_serve_optimize_schedule_uses_deterministic_fallback_after_failed_retry(tmp_path, monkeypatch):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    schedule_path = web_dir / "schedule_data.json"
    schedule_path.write_text(json.dumps({
        "locations": {
            "Supreme HQ, Bandra": [{
                "location": "Supreme HQ, Bandra",
                "day_of_week": "Monday",
                "time": "09:00",
                "class_name": "Studio Barre 57",
                "trainer_1": "Trainer A",
                "score": 20,
                "predicted_fill_rate": 0.2,
            }]
        }
    }))
    config_path = tmp_path / "schedule_config.json"
    config_path.write_text(json.dumps({"settings_options": {"ai_optimize_api_key": "test-key"}}))
    profiles_path = tmp_path / "trainer_profiles.json"
    profiles_path.write_text(json.dumps([
        {"name": "Trainer A", "tier": 1, "qualifications": {"barre_57": True}, "locations": {"Supreme HQ, Bandra": {"available_days": ["Monday"], "time_window": {"start": "06:00", "end": "22:00"}, "max_classes_per_day": 4}}},
        {"name": "Trainer B", "tier": 1, "qualifications": {"barre_57": True}, "locations": {"Supreme HQ, Bandra": {"available_days": ["Monday"], "time_window": {"start": "06:00", "end": "22:00"}, "max_classes_per_day": 4}}},
    ]))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "03_scores.json").write_text(json.dumps({"class_slot_ranking": [{
        "location": "Supreme HQ, Bandra",
        "day_name": "Monday",
        "time": "09:00",
        "class": "Studio Barre 57",
        "trainer": "Trainer B",
        "score": 90,
        "avg_fill_rate": 0.8,
        "avg_checkin": 12,
        "session_count": 8,
    }]}))

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "operations": [{"type": "change_class", "id": "1", "new_class": "Studio Cardio Barre", "reason": "invalid"}]
            })}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            return FakeResponse()

    class FakeTimeout:
        def __init__(self, *args, **kwargs):
            pass

    class FakeHttpx:
        Client = FakeClient
        Timeout = FakeTimeout
        TimeoutException = TimeoutError

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)
    monkeypatch.setattr(serve_module, "WEB_DIR", web_dir)
    monkeypatch.setattr(serve_module, "SCHEDULE_CONFIG_PATH", config_path)
    monkeypatch.setattr(serve_module, "TRAINER_PROFILES_PATH", profiles_path)
    monkeypatch.setattr(serve_module, "STATE_DIR", state_dir)

    result = serve_module._run_optimize_with_ai({"location": "Supreme HQ, Bandra"})

    assert result["applied_count"] == 1
    assert result["applied"][0]["type"] == "swap_trainer"
    assert json.loads(schedule_path.read_text())["locations"]["Supreme HQ, Bandra"][0]["trainer_1"] == "Trainer B"
    assert "Deterministic fallback" in result["summary"]


def test_optimizer_allows_change_class_with_exact_better_history(monkeypatch):
    monkeypatch.setattr(serve_module, "_validate_manual_slot", lambda *args, **kwargs: None)
    slot = {
        "location": "Kwality House, Kemps Corner",
        "day_of_week": "Monday",
        "time": "11:00",
        "class_name": "Studio Cardio Barre Express",
        "trainer_1": "Trainer A",
        "predicted_fill_rate": 0.10,
        "score": 35,
    }
    schedule_data = {"locations": {"Kwality House, Kemps Corner": [slot]}}
    compact = [{"id": "1", "loc": "Kwality House, Kemps Corner", "schedule_index": 0, "slot": slot}]

    applied, rejected = serve_module._apply_ai_schedule_operations(
        schedule_data,
        [{"type": "change_class", "id": "1", "new_class": "Studio Cardio Barre", "reason": "better format"}],
        compact,
        {"Trainer A"},
        "Main",
        {"class_slot_ranking": [{
            "location": "Kwality House, Kemps Corner",
            "day": "Monday",
            "time": "11:00",
            "class": "Studio Cardio Barre",
            "trainer": "Trainer A",
            "fill": 0.55,
            "checkins": 9,
            "sessions": 6,
            "score": 80,
        }]},
        {},
    )

    assert rejected == []
    assert applied[0]["after"]["class_name"] == "Studio Cardio Barre"


def test_serve_static_svg_uses_image_mime_type():
    assert serve_module.MIME_TYPES[".svg"] == "image/svg+xml"


def test_web_template_exposes_week_picker_and_two_generate_modes():
    template = web_ui_source()

    assert 'id="schedule-week-start"' in template
    assert "Generate with AI" in template
    assert "runPipelineFromHeader(false)" in template
    assert "runPipelineFromHeader(true)" in template
    assert "AI API Key" in template


def test_rooms_view_uses_active_location_only_and_shows_utilisation():
    template = web_ui_source()

    assert 'renderRoomView(area,filtered);' in template
    assert 'const activeLocation=_loc||filtered[0]?.location||"";' in template
    assert 'utilizationPct' in template
    assert 'util</span>' in template


def test_settings_console_supports_editing_custom_rules_and_manual_pins():
    template = web_ui_source()

    assert 'onclick="settEditCustomRule(' in template
    assert 'onclick="settCancelCustomRuleEdit()"' in template
    assert 'onclick="settEditManualPin(' in template
    assert 'onclick="settCancelManualPinEdit()"' in template


def test_strength_lab_above_50_fill_is_protected():
    assert is_protected_strength_lab_row({
        "class": "Studio Strength Lab (Push)",
        "avg_fill_rate": 0.51,
        "session_count": 3,
    })
    assert not is_protected_strength_lab_row({
        "class": "Studio Strength Lab (Push)",
        "avg_fill_rate": 0.50,
        "session_count": 3,
    })
    assert not is_protected_strength_lab_row({
        "class": "Studio Barre 57",
        "avg_fill_rate": 0.80,
        "session_count": 3,
    })


def test_top_performer_above_50_fill_with_history_is_protected():
    assert _is_top_performer_protected("Studio PowerCycle", 3.0, 8.0, 0.5524, 120)
    assert not _is_top_performer_protected("Studio PowerCycle", 20.0, 8.0, 0.50, 120)
    assert not _is_top_performer_protected("Studio PowerCycle", 20.0, 8.0, 0.70, 7)
    assert _is_top_performer_protected("Studio Mat 57", 11.03, 8.0, 0.30, 120)
    assert _is_top_performer_protected("Studio Barre 57", 7.95, 5.85, 0.6084, 60)
    assert not _is_top_performer_protected("Studio Barre 57", 6.64, 5.85, 0.414, 25)
    assert not _is_top_performer_protected("Studio Mat 57", 7.5, 8.0, 0.90, 120)


def test_class_difficulty_level_groups_member_options():
    assert class_difficulty_level("Studio Mat 57 Express") == "beginner"
    assert class_difficulty_level("Studio Barre 57") == "intermediate"
    assert class_difficulty_level("Studio FIT") == "advanced"


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
    assert entry["schedule_score"] == 80.0


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


def test_reporter_weekly_cap_assertion_includes_derived_locations(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    reporter = OutputReporter()

    def slot(location, trainer, idx):
        return make_slot(
            location=location,
            trainer_1=trainer,
            time="12:30" if location == "Courtside" else "09:00",
            duration_min=57,
        ).__dict__

    by_location = {
        "Kwality House, Kemps Corner": [
            slot("Kwality House, Kemps Corner", "Overloaded Trainer" if i < 15 else f"KW Trainer {i}", i)
            for i in range(60)
        ],
        "Supreme HQ, Bandra": [
            slot("Supreme HQ, Bandra", f"SU Trainer {i}", i)
            for i in range(55)
        ],
        "Kenkere House": [
            slot("Kenkere House", f"KE Trainer {i}", i)
            for i in range(45)
        ],
        "Courtside": [
            slot("Courtside", "Overloaded Trainer", 0)
        ],
    }
    scorecard = {
        "locations": {
            loc: {
                "total_classes": len(slots),
                "schedule_score": 100.0,
                "format_counts": {},
                "barre_family_pct": 0.30,
                "barre_family_count": 20,
            }
            for loc, slots in by_location.items()
        }
    }

    reporter._run_assertions(scorecard, by_location)
    output = capsys.readouterr().out

    assert "Overloaded Trainer weekly load" in output


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

    assert [group["id"] for group in catalog["groups"]] == ["universal"]
    assert all_rules
    assert all(rule.get("impact_area") for rule in all_rules)
    assert all(rule.get("risk_level") in {"critical", "high", "medium", "low"} for rule in all_rules)
    assert all(rule.get("status_tag") in {"Recommended", "Risky", "Disabled"} for rule in all_rules)
    assert not any(rule.get("type") == "class_format" for rule in all_rules)


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


def test_trainer_cannot_exceed_tier_based_weekly_cap():
    # Tier 1 is capped at 15h/week.
    state = TrainerState("Anisha Shah", 1)
    assert state.max_weekly_minutes == 15 * 60
    state.weekly_minutes = state.max_weekly_minutes - get_class_duration("Studio Barre 57") + 1
    assert not state.can_add("Monday", "09:00", "Kwality House, Kemps Corner", "Studio Barre 57", 4, "07:00", "20:30")

    # Karan is T2, cap is 15h
    state2 = TrainerState("Karan Bhatia", 2)
    state2.weekly_minutes = state2.max_weekly_minutes - get_class_duration("Studio Barre 57") + 1
    assert not state2.can_add("Monday", "09:00", "Kwality House, Kemps Corner", "Studio Barre 57", 4, "07:00", "20:30")


def test_trainer_cannot_exceed_four_hours_per_day():
    state = TrainerState("Tier One", 1)
    for time_str in ("07:00", "08:00", "09:00", "10:00"):
        assert state.can_add("Monday", time_str, "Kwality House, Kemps Corner", "Studio Barre 57", 8, "07:00", "20:30")
        state.add("Monday", time_str, "Kwality House, Kemps Corner", "Studio Barre 57")

    assert state.weekly_minutes < state.max_weekly_minutes
    assert not state.can_add("Monday", "11:00", "Kwality House, Kemps Corner", "Studio Barre 57", 8, "07:00", "20:30")


def test_trainer_cannot_be_assigned_all_seven_days():
    # CLAUDE.md: max 5 work days per trainer.
    state = TrainerState("Tier One", 1)
    for day in DAY_ORDER[:5]:
        assert state.can_add(day, "07:00", "Kwality House, Kemps Corner", "Studio Barre 57 Express", 8, "07:00", "20:30")
        state.add(day, "07:00", "Kwality House, Kemps Corner", "Studio Barre 57 Express")

    assert not state.can_add("Saturday", "10:00", "Kwality House, Kemps Corner", "Studio Barre 57 Express", 8, "07:00", "20:30")
    assert not state.can_add("Sunday", "10:00", "Kwality House, Kemps Corner", "Studio Barre 57 Express", 8, "07:00", "20:30")


def test_sunday_blocks_early_start_but_allows_pm_when_targets_require_it():
    assert slot_is_in_blocked_window("Sunday", "09:45")
    assert not slot_is_in_blocked_window("Sunday", "10:00")
    assert not slot_is_in_blocked_window("Sunday", "16:00")
    assert not slot_is_in_blocked_window("Sunday", "18:00")


def test_weekly_cap_enforcement_does_not_drop_day_below_configured_floor():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    location = "Kwality House, Kemps Corner"
    optimiser.schedule_config = {
        "targets": {
            location: {
                "Monday": {"target": 2, "min": 2},
                "Sunday": {"target": 6, "min": 6},
            }
        }
    }
    optimiser._weekly_target_for_location = lambda loc: 7
    optimiser._weekly_location_overflow_minutes = {location: 0}
    all_slots = [
        make_slot(location=location, day_of_week="Monday", time=f"0{idx}:00", trainer_1=f"Mon {idx}", score=10 + idx)
        for idx in range(4)
    ] + [
        make_slot(location=location, day_of_week="Sunday", time=f"1{idx}:00", trainer_1=f"Sun {idx}", score=1)
        for idx in range(6)
    ]

    optimiser._enforce_location_weekly_cap(location, all_slots, weekly_class_counts={})

    assert sum(1 for slot in all_slots if slot.day_of_week == "Sunday") == 6
    assert len(all_slots) > 7


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


def test_tier1_target_is_thirteen_hours_before_lower_tier_priority():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.trainer_states = {
        "Tier One": TrainerState("Tier One", 1),
        "Tier Two": TrainerState("Tier Two", 2),
    }
    assert TIER1_WEEKLY_TARGET_MIN == 13 * 60

    optimiser.trainer_states["Tier One"].weekly_minutes = 12 * 60
    optimiser.trainer_states["Tier Two"].weekly_minutes = 0

    assert optimiser._tier_priority_score("Tier One") > optimiser._tier_priority_score("Tier Two")

    optimiser.trainer_states["Tier One"].weekly_minutes = 13 * 60
    assert optimiser.trainer_states["Tier One"].at_weekly_target()


def test_tier1_new_workday_priority_pushes_toward_five_assigned_days():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    state = TrainerState("Tier One", 1)
    for day in DAY_ORDER[:3]:
        state.add(day, "08:00", "Kwality House, Kemps Corner", "Studio Barre 57 Express")
    optimiser.trainer_states = {"Tier One": state}

    assert optimiser._trainer_hours_bonus("Tier One", "Thursday") > optimiser._trainer_hours_bonus("Tier One", "Monday") + 50


def test_mumbai_tier1_supreme_band_balances_bandra_and_kwality_load():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.trainer_profiles = {
        "Tier One": {
            "name": "Tier One",
            "locations": {
                "Supreme HQ, Bandra": {
                    "available_days": DAY_ORDER,
                    "max_classes_per_day": 4,
                },
                "Kwality House, Kemps Corner": {
                    "available_days": DAY_ORDER,
                    "max_classes_per_day": 4,
                },
            },
        }
    }
    optimiser.trainer_home_region = {"Tier One": "mumbai"}
    optimiser.trainer_states = {"Tier One": TrainerState("Tier One", 1)}

    min_supreme, max_supreme = optimiser._mumbai_tier1_supreme_band("Tier One")

    assert min_supreme >= 6 * get_class_duration("Studio Barre 57")
    assert max_supreme >= 7 * get_class_duration("Studio Barre 57")
    assert optimiser._location_tier_priority_score("Tier One", "Supreme HQ, Bandra") > 300
    assert optimiser._location_tier_priority_score("Tier One", "Kwality House, Kemps Corner") < -250


def test_supreme_pins_expand_tier1_bandra_band_when_pinned_classes_say_so():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.trainer_profiles = {
        "Tier One": {
            "name": "Tier One",
            "locations": {
                "Supreme HQ, Bandra": {
                    "available_days": DAY_ORDER,
                    "max_classes_per_day": 4,
                },
                "Kwality House, Kemps Corner": {
                    "available_days": DAY_ORDER,
                    "max_classes_per_day": 4,
                },
            },
        }
    }
    optimiser.trainer_home_region = {"Tier One": "mumbai"}
    optimiser.trainer_states = {"Tier One": TrainerState("Tier One", 1)}
    pinned_minutes = 9 * get_class_duration("Studio Barre 57")
    optimiser._supreme_pinned_minutes_total = {"Tier One": pinned_minutes}
    optimiser._kwality_pinned_minutes_total = {}

    min_supreme, max_supreme = optimiser._mumbai_tier1_supreme_band("Tier One")

    assert min_supreme >= pinned_minutes
    assert max_supreme >= pinned_minutes


def test_mumbai_tier1_balance_blocks_non_pinned_supreme_over_cap():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.trainer_profiles = {
        "Tier One": {
            "name": "Tier One",
            "locations": {
                "Supreme HQ, Bandra": {
                    "available_days": DAY_ORDER,
                    "max_classes_per_day": 4,
                },
                "Kwality House, Kemps Corner": {
                    "available_days": DAY_ORDER,
                    "max_classes_per_day": 4,
                },
            },
        }
    }
    optimiser.trainer_home_region = {"Tier One": "mumbai"}
    state = TrainerState("Tier One", 1)
    for idx in range(8):
        state.add(DAY_ORDER[idx % len(DAY_ORDER)], f"09:{idx:02d}", "Supreme HQ, Bandra", "Studio Barre 57")
    optimiser.trainer_states = {"Tier One": state}
    optimiser._supreme_pinned_minutes_total = {}
    optimiser._kwality_pinned_minutes_total = {}

    assert not optimiser._mumbai_tier1_balance_allows_assignment(
        "Tier One",
        "Supreme HQ, Bandra",
        "Studio Barre 57",
    )
    assert optimiser._mumbai_tier1_balance_allows_assignment(
        "Tier One",
        "Kwality House, Kemps Corner",
        "Studio Barre 57",
    )


def test_limited_supreme_availability_caps_tier1_bandra_reservation():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.trainer_profiles = {
        "Thursday Tier One": {
            "name": "Thursday Tier One",
            "locations": {
                "Supreme HQ, Bandra": {
                    "available_days": ["Thursday"],
                    "max_classes_per_day": 4,
                },
                "Kwality House, Kemps Corner": {
                    "available_days": DAY_ORDER,
                    "max_classes_per_day": 4,
                },
            },
        }
    }
    optimiser.trainer_home_region = {"Thursday Tier One": "mumbai"}
    optimiser.trainer_states = {"Thursday Tier One": TrainerState("Thursday Tier One", 1)}

    min_supreme, max_supreme = optimiser._mumbai_tier1_supreme_band("Thursday Tier One")

    assert min_supreme == 4 * get_class_duration("Studio Barre 57")
    assert max_supreme == 4 * get_class_duration("Studio Barre 57")


def test_available_tier1_trainers_for_supreme_slot_excludes_busy_and_unqualified():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.trainer_profiles = {
        "Tier One": {
            "name": "Tier One",
            "locations": {
                "Supreme HQ, Bandra": {
                    "available_days": ["Monday"],
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 4,
                }
            },
            "qualifications": {"all_barre": True, "powercycle": True},
        },
        "Busy Tier One": {
            "name": "Busy Tier One",
            "locations": {
                "Supreme HQ, Bandra": {
                    "available_days": ["Monday"],
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 4,
                }
            },
            "qualifications": {"all_barre": True, "powercycle": True},
        },
        "Unqualified Tier One": {
            "name": "Unqualified Tier One",
            "locations": {
                "Supreme HQ, Bandra": {
                    "available_days": ["Monday"],
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 4,
                }
            },
            "qualifications": {"all_barre": True, "powercycle": False},
        },
    }
    optimiser.trainer_states = {
        "Tier One": TrainerState("Tier One", 1),
        "Busy Tier One": TrainerState("Busy Tier One", 1),
        "Unqualified Tier One": TrainerState("Unqualified Tier One", 1),
    }
    optimiser.trainer_states["Busy Tier One"].add("Monday", "09:00", "Supreme HQ, Bandra", "Studio Barre 57")

    assert optimiser._available_tier1_trainers_for_slot(
        "Supreme HQ, Bandra",
        "Monday",
        "2026-05-04",
        "09:00",
        "Studio PowerCycle",
        {"Busy Tier One"},
    ) == ["Tier One"]


def test_location_planning_order_reserves_bandra_before_kwality():
    optimiser = ScheduleOptimiser(
        target_week_start="2026-05-04",
        locations=["Kwality House, Kemps Corner", "Supreme HQ, Bandra", "Kenkere House"],
    )

    assert optimiser._location_planning_order()[:2] == [
        "Supreme HQ, Bandra",
        "Kwality House, Kemps Corner",
    ]

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


def test_ai_iteration_selector_accepts_daily_counts_inside_saved_range(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "schedule_config.json").write_text(json.dumps({
        "targets": {
            "Kwality House, Kemps Corner": {
                "Monday": {"target": 10, "max": 14}
            }
        }
    }))

    schedule = [
        {"location": "Kwality House, Kemps Corner", "day_of_week": "Monday"}
        for _ in range(12)
    ]

    assert _daily_target_errors(schedule) == []


def test_fallback_iteration_selector_prefers_tier1_hour_compliance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rules").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "rules" / "trainer_profiles.json").write_text(json.dumps([
        {"name": "Tier One", "tier": 1, "active": True},
        {"name": "Tier Two", "tier": 2, "active": True},
    ]))
    (tmp_path / "config" / "schedule_config.json").write_text(json.dumps({}))

    underloaded = {
        "iteration_name": "underloaded",
        "schedule": [
            {"trainer_1": "Tier One", "duration_min": 60}
            for _ in range(12)
        ] + [
            {"trainer_1": "Tier Two", "duration_min": 60}
        ],
    }
    compliant = {
        "iteration_name": "compliant",
        "schedule": [
            {"trainer_1": "Tier One", "duration_min": 60}
            for _ in range(13)
        ],
    }

    assert _select_primary_iteration([underloaded, compliant])["iteration_name"] == "compliant"


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


def test_non_kwality_assignment_cannot_block_same_day_tier1_kwality_manual_pin():
    kwality = "Kwality House, Kemps Corner"
    supreme = "Supreme HQ, Bandra"
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.schedule_config = {
        "manual_protected": [
            {
                "location": kwality,
                "day": "Thursday",
                "time": "09:15",
                "class": "Studio Barre 57",
                "trainer": "Tier One Trainer",
                "enabled": True,
            }
        ]
    }
    optimiser.trainer_profiles = {
        "Tier One Trainer": {
            "name": "Tier One Trainer",
            "tier": 1,
            "locations": {
                kwality: {
                    "available_days": ["Thursday", "Friday"],
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 4,
                },
                supreme: {
                    "available_days": ["Thursday", "Friday"],
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 4,
                },
            },
            "qualifications": {"all_barre": True},
        }
    }
    optimiser.trainer_states = {"Tier One Trainer": TrainerState("Tier One Trainer", 1)}
    optimiser.class_family = {"Studio Barre 57": "barre_57"}
    optimiser.overrides = {}
    optimiser._pinned_minutes_remaining = {"Tier One Trainer": 57}

    assert not optimiser._trainer_ok("Tier One Trainer", supreme, "Thursday", "10:30", "Studio Barre 57")
    assert optimiser._trainer_ok("Tier One Trainer", supreme, "Friday", "10:30", "Studio Barre 57")


def test_lower_tier_trainer_is_not_blocked_by_kwality_pin_priority_guard():
    kwality = "Kwality House, Kemps Corner"
    supreme = "Supreme HQ, Bandra"
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.schedule_config = {
        "manual_protected": [
            {
                "location": kwality,
                "day": "Thursday",
                "time": "09:15",
                "class": "Studio Barre 57",
                "trainer": "Tier Two Trainer",
                "enabled": True,
            }
        ]
    }
    optimiser.trainer_profiles = {
        "Tier Two Trainer": {
            "name": "Tier Two Trainer",
            "tier": 2,
            "locations": {
                kwality: {
                    "available_days": ["Thursday"],
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 4,
                },
                supreme: {
                    "available_days": ["Thursday"],
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 4,
                },
            },
            "qualifications": {"all_barre": True},
        }
    }
    optimiser.trainer_states = {"Tier Two Trainer": TrainerState("Tier Two Trainer", 2)}
    optimiser.class_family = {"Studio Barre 57": "barre_57"}
    optimiser.overrides = {}
    optimiser._pinned_minutes_remaining = {"Tier Two Trainer": 57}

    assert optimiser._trainer_ok("Tier Two Trainer", supreme, "Thursday", "10:30", "Studio Barre 57")


def test_non_kwality_manual_pin_is_released_when_kwality_pin_has_priority():
    kwality = "Kwality House, Kemps Corner"
    supreme = "Supreme HQ, Bandra"
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.schedule_config = {
        "manual_protected": [
            {
                "location": supreme,
                "day": "Saturday",
                "time": "11:30",
                "class": "Studio Barre 57",
                "trainer": "Rohan Dahima",
                "enabled": True,
            },
            {
                "location": kwality,
                "day": "Saturday",
                "time": "11:30",
                "class": "Studio Barre 57",
                "trainer": "Rohan Dahima",
                "enabled": True,
            },
        ]
    }
    optimiser.trainer_profiles = {
        "Rohan Dahima": {
            "name": "Rohan Dahima",
            "tier": 1,
            "locations": {
                supreme: {
                    "available_days": ["Saturday"],
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 4,
                }
            },
            "qualifications": {"all_barre": True},
        }
    }
    optimiser.trainer_states = {"Rohan Dahima": TrainerState("Rohan Dahima", 1)}
    optimiser.class_family = {"Studio Barre 57": "barre_57"}
    optimiser.overrides = {}
    optimiser._pinned_minutes_remaining = {"Rohan Dahima": 114}
    optimiser.protected = {}
    optimiser.protected_class_times = {}
    optimiser._time_class_counts = {}
    optimiser._time_format_counts = {}
    optimiser._time_level_counts = {}
    optimiser.scores_data = {}

    slots = optimiser._schedule_day(
        supreme,
        "Saturday",
        "2026-05-09",
        target_count=0,
        day_max=0,
        am_slots=[],
        pm_slots=[],
        room_occ=RoomOccupancy({"studio_a": {"capacity": 14, "families": None}}),
        weekly_class_counts={},
    )

    assert slots == []
    assert optimiser._pinned_minutes_remaining["Rohan Dahima"] == 57


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


def test_courtside_targets_follow_configured_weekday_settings():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.schedule_config = {
        "targets": {
            "Courtside": {
                "Monday": {"target": 1, "min": 1, "max": 1},
                "Saturday": {"target": 2, "min": 2, "max": 2},
            }
        }
    }

    assert optimiser._pick_daily_target("Courtside", "Monday") == 1
    assert optimiser._location_allowed_day("Courtside", "Monday")


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

    assert "  Monday: 13-14" in prompt
    assert "  Sunday: 4-6" in prompt
    assert "  WEEK TOTAL RANGE: 78-88" in prompt
    assert "Hit saved daily targets" not in prompt


def test_ai_location_prompt_exposes_weekly_bounds_not_exact_floor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "schedule_config.json").write_text(json.dumps({
        "targets": {
            "Kwality House, Kemps Corner": {
                "Monday": {"target": 11, "max": 14},
                "Tuesday": {"target": 10, "max": 14},
                "Wednesday": {"target": 10, "max": 14},
                "Thursday": {"target": 11, "max": 15},
                "Friday": {"target": 10, "max": 14},
                "Saturday": {"target": 12, "max": 17},
                "Sunday": {"target": 6, "max": 10},
            }
        }
    }))

    prompt = _build_location_prompt(
        "Kwality House, Kemps Corner",
        "2026-05-04",
        {"class_slot_ranking": []},
        {"trainer_metrics": [], "day_band_metrics": []},
        profiles=[],
    )

    assert "WEEK TOTAL RANGE: 70-98" in prompt
    assert "Do not force the lower bound as an exact weekly count" in prompt


def test_weekly_location_target_varies_by_seed_and_stays_inside_bounds():
    picked = {
        ScheduleOptimiser(target_week_start="2026-05-04", locations=[], variation_seed=seed)._weekly_target_for_location(
            "Kwality House, Kemps Corner"
        )
        for seed in [1, 2, 3, 4, 5, 42, 137, 999]
    }

    assert all(70 <= value <= 80 for value in picked)
    assert len(picked) > 1


def test_weekly_location_target_does_not_anchor_main_studio_to_floor():
    for seed in range(1, 40):
        target = ScheduleOptimiser(
            target_week_start="2026-05-04",
            locations=[],
            variation_seed=seed,
        )._weekly_target_for_location("Kwality House, Kemps Corner")

        assert 70 < target <= 80


def test_final_weekly_floor_repair_uses_selected_weekly_target_for_main_studio():
    optimiser = ScheduleOptimiser(
        target_week_start="2026-05-04",
        locations=["Kwality House, Kemps Corner"],
        variation_seed=54365,
    )
    optimiser.schedule_config = {
        "targets": {
            "Kwality House, Kemps Corner": {
                day: {"target": 0, "max": 20}
                for day in DAY_ORDER
            }
        },
        "settings_options": {
            "location_weekly_floors": {"Kwality House, Kemps Corner": 70}
        },
    }
    optimiser.class_family = {"Studio Barre 57": "barre_57"}
    optimiser.trainer_profiles = {
        "Repair Trainer": {
            "tier": 1,
            "locations": {
                "Kwality House, Kemps Corner": {
                    "available_days": DAY_ORDER,
                    "time_window": {"start": "06:00", "end": "22:00"},
                    "max_classes_per_day": 4,
                }
            },
        }
    }
    optimiser.trainer_states = {"Repair Trainer": TrainerState("Repair Trainer", 1)}
    optimiser.hist_lookup = {}
    optimiser._hist_by_combo_day = {}
    optimiser._hist_slot_cache = {}
    optimiser._hist_slot_exact = {}
    optimiser._hist_slot_nearby = {}
    optimiser._hist_by_slot_day = {}
    optimiser._hist_class_day = {}
    optimiser._hist_class_any = {}
    optimiser._get_hist = lambda *args, **kwargs: {}
    existing = [
        make_slot(
            date="2026-05-04",
            day_of_week="Monday",
            time=f"05:{idx:02d}",
            trainer_1=f"Existing Trainer {idx}",
        )
        for idx in range(70)
    ]

    optimiser._final_weekly_floor_repair(existing, date.fromisoformat("2026-05-04"))

    assert len(existing) == optimiser._weekly_target_for_location("Kwality House, Kemps Corner")
    assert len(existing) > 70


def test_final_weekly_floor_repair_does_not_duplicate_same_class_same_time():
    optimiser = ScheduleOptimiser(
        target_week_start="2026-05-04",
        locations=["Kwality House, Kemps Corner"],
        variation_seed=54365,
    )
    optimiser.schedule_config = {
        "targets": {
            "Kwality House, Kemps Corner": {
                day: {"target": 2, "max": 2}
                for day in DAY_ORDER
            }
        }
    }
    optimiser.schedule_config["targets"]["Kwality House, Kemps Corner"]["Saturday"] = {
        "target": 1,
        "max": 3,
    }
    optimiser._weekly_target_for_location = lambda location: 14
    optimiser.class_family = {
        "Studio FIT": "fit",
        "Studio Cardio Barre": "cardio_barre",
        "Studio Mat 57": "mat_57",
        "Studio Barre 57": "barre_57",
        "Studio Barre 57 Express": "barre_57",
        "Studio Cardio Barre Express": "cardio_barre",
        "Studio Mat 57 Express": "mat_57",
    }
    optimiser.trainer_profiles = {
        "Repair Trainer": {
            "tier": 1,
            "locations": {
                "Kwality House, Kemps Corner": {
                    "available_days": DAY_ORDER,
                    "time_window": {"start": "06:00", "end": "22:00"},
                    "max_classes_per_day": 4,
                }
            },
        }
    }
    optimiser.trainer_states = {"Repair Trainer": TrainerState("Repair Trainer", 1)}
    optimiser.hist_lookup = {}
    optimiser._get_hist = lambda *args, **kwargs: {}

    existing = []
    for day in DAY_ORDER:
        if day == "Saturday":
            existing.append(make_slot(
                day_of_week="Saturday",
                date="2026-05-09",
                time="07:00",
                class_name="Studio FIT",
                trainer_1="Existing Trainer",
                room="studio_a",
            ))
            continue
        existing.extend([
            make_slot(day_of_week=day, time="05:00", trainer_1=f"{day} A"),
            make_slot(day_of_week=day, time="05:10", trainer_1=f"{day} B", room="studio_b"),
        ])

    optimiser._final_weekly_floor_repair(existing, date.fromisoformat("2026-05-04"))

    saturday_7_fit = [
        slot for slot in existing
        if slot.day_of_week == "Saturday"
        and slot.time == "07:00"
        and slot.class_name == "Studio FIT"
    ]
    assert len(existing) == 14
    assert len(saturday_7_fit) == 1
    assert all(
        slot_time_to_minutes(entry["time"]) >= slot_time_to_minutes("08:30")
        for entry in optimiser._floor_repair_log
    )


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


def test_kwality_protected_class_times_can_share_clock_time_when_rooms_are_free():
    location = "Kwality House, Kemps Corner"
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.schedule_config = {"class_mix": {location: {}}, "custom_rules": []}
    optimiser.class_family = {
        "Studio Mat 57": "barre_57",
        "Studio Barre 57": "barre_57",
    }
    optimiser.protected = {}
    optimiser.protected_class_times = {
        (location, 0): [
            {
                "location": location,
                "day": 0,
                "time": "10:15",
                "class": "Studio Mat 57",
                "score": 90.0,
                "top_trainers": [{"trainer": "Trainer Mat"}],
            },
            {
                "location": location,
                "day": 0,
                "time": "10:15",
                "class": "Studio Barre 57",
                "score": 85.0,
                "top_trainers": [{"trainer": "Trainer Barre"}],
            },
        ]
    }
    optimiser.scores_data = {
        "class_slot_ranking": [
            {
                "location": location,
                "day": 0,
                "time": "10:15",
                "class": "Studio Mat 57",
                "trainer": "Trainer Mat",
                "score": 90.0,
                "recommendation": "PROTECT",
                "session_count": 12,
            },
            {
                "location": location,
                "day": 0,
                "time": "10:15",
                "class": "Studio Barre 57",
                "trainer": "Trainer Barre",
                "score": 85.0,
                "recommendation": "PROTECT",
                "session_count": 12,
            },
        ],
        "slot_group_ranking": [],
    }
    optimiser._build_score_indexes()
    optimiser.hist_lookup = {
        (location, "Studio Mat 57", "Trainer Mat", 0, "10:15"): {
            "session_count": 12,
            "avg_fill_rate": 0.75,
            "avg_checkin": 15.0,
        },
        (location, "Studio Barre 57", "Trainer Barre", 0, "10:15"): {
            "session_count": 12,
            "avg_fill_rate": 0.70,
            "avg_checkin": 14.0,
        },
    }
    optimiser._build_history_indexes()
    optimiser.trainer_states = {
        "Trainer Mat": TrainerState("Trainer Mat", 1),
        "Trainer Barre": TrainerState("Trainer Barre", 1),
    }
    optimiser.trainer_profiles = {
        name: {
            "name": name,
            "tier": 1,
            "locations": {
                location: {
                    "available_days": ["Monday"],
                    "time_window": {"start": "07:00", "end": "22:00"},
                    "max_classes_per_day": 4,
                }
            },
            "qualifications": {"all_barre": True},
        }
        for name in ("Trainer Mat", "Trainer Barre")
    }
    optimiser.trainer_home_region = {}
    optimiser._ai_boost = {}
    optimiser._ai_penalty = {}
    optimiser._ai_mix_boost = {}

    slots = optimiser._schedule_day(
        location,
        "Monday",
        "2026-05-04",
        target_count=2,
        day_max=2,
        am_slots=["10:15"],
        pm_slots=[],
        room_occ=RoomOccupancy({
            "strength_lab": {"capacity": 7, "families": ["strength_lab"]},
            "powercycle": {"capacity": 10, "families": ["powercycle"]},
            "studio_a": {"capacity": 22, "families": None},
            "studio_b": {"capacity": 13, "families": None},
        }),
        weekly_class_counts={},
    )

    same_time = [slot for slot in slots if slot.time == "10:15"]
    assert {slot.class_name for slot in same_time} == {"Studio Mat 57", "Studio Barre 57"}
    assert {slot.room for slot in same_time} == {"studio_a", "studio_b"}


def test_consecutive_format_check_allows_parallel_distinct_classes_at_same_time():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    slots_today = [
        make_slot(time="17:45", class_name="Studio Barre 57", room="studio_a")
    ]

    assert not optimiser._would_repeat_consecutive_format(
        slots_today,
        "17:45",
        "Studio Barre 57 Express",
    )
    assert optimiser._same_class_already_at_time(
        slots_today,
        "17:45",
        "Studio Barre 57",
    )


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


def test_optimiser_history_slot_lookup_uses_canonical_class_family_before_fallback():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.hist_lookup = {
        ("Kwality House, Kemps Corner", "Studio Barre 57 Express", "Trainer A", 0, "17:45"): {
            "session_count": 4,
            "avg_fill_rate": 0.70,
            "avg_checkin": 9.0,
        },
        ("Kwality House, Kemps Corner", "Studio Barre 57", "Trainer B", 0, "18:45"): {
            "session_count": 6,
            "avg_fill_rate": 0.80,
            "avg_checkin": 12.0,
        },
        ("Kwality House, Kemps Corner", "Studio HIIT", "Trainer C", 0, "17:45"): {
            "session_count": 10,
            "avg_fill_rate": 0.95,
            "avg_checkin": 15.0,
        },
    }

    optimiser._build_history_indexes()
    hist = optimiser._get_hist_slot("Kwality House, Kemps Corner", "Barre 57", 0, "17:45")

    assert hist["session_count"] == 4
    assert hist["avg_fill_rate"] == pytest.approx(0.70)
    assert hist["avg_checkin"] == pytest.approx(9.0)
    assert optimiser._evidence_adjusted_fill(hist) == pytest.approx(0.70)


def test_optimiser_history_slot_lookup_does_not_cross_canonical_class_families():
    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    optimiser.hist_lookup = {
        ("Kwality House, Kemps Corner", "Studio Barre 57", "Trainer A", 0, "17:45"): {
            "session_count": 8,
            "avg_fill_rate": 0.70,
            "avg_checkin": 9.0,
        }
    }

    optimiser._build_history_indexes()
    hist = optimiser._get_hist_slot("Kwality House, Kemps Corner", "Studio HIIT", 0, "17:45")

    assert hist == {}


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


def test_ai_only_repairs_invalid_location_plan_instead_of_failing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "rules").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "state" / "03_scores.json").write_text(json.dumps({
        "class_slot_ranking": [],
        "slot_group_ranking": [],
    }))
    (tmp_path / "state" / "02_metrics.json").write_text(json.dumps({
        "trainer_metrics": [],
        "day_band_metrics": [],
    }))
    (tmp_path / "rules" / "trainer_profiles.json").write_text(json.dumps([
        {
            "name": "Trainer A",
            "tier": 1,
            "active": True,
            "qualifications": {"all_barre": True},
            "locations": {
                "Kwality House, Kemps Corner": {
                    "available_days": ["Monday"],
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 4,
                }
            },
        }
    ]))
    (tmp_path / "config" / "rules_config.json").write_text(json.dumps({
        "categories": {"universal": {"enabled": True}},
        "rules": {},
    }))
    (tmp_path / "config" / "schedule_config.json").write_text(json.dumps({}))

    import agents.ai_planner as ai_planner_module

    monkeypatch.setenv("SCHEDULER_FORCE_AI_ONLY", "1")
    monkeypatch.setattr(ai_planner_module, "OPENAI_AVAILABLE", True)
    monkeypatch.setattr(ai_planner_module, "create_ai_client", lambda: (object(), {"model": "test-model"}))
    monkeypatch.setattr(
        AISchedulePlanner,
        "_call_model",
        lambda self, client, model_name, system_prompt, user_prompt, location, max_tokens=None: '{"schedule":[]}',
    )

    repaired_slot = PlannedSlot(
        location="Kwality House, Kemps Corner",
        date="2026-05-04",
        day_of_week="Monday",
        time="09:00",
        class_name="Studio Barre 57",
        trainer_1="Trainer A",
        trainer_2="",
        cover="",
        room="studio_a",
        capacity=20,
        predicted_fill_rate=0.5,
        score=80.0,
        constraint_violations=[],
        rationale="greedy_fallback",
    )
    monkeypatch.setattr(
        AISchedulePlanner,
        "_fallback_location",
        lambda self, location, scores_data, profiles_by_name: [repaired_slot],
    )

    planner = AISchedulePlanner(
        target_week_start="2026-05-04",
        locations=["Kwality House, Kemps Corner"],
    )

    output = planner.run()

    assert output["schedule"][0]["trainer_1"] == "Trainer A"
    assert output["ai_planned"] is True
    assert output["ai_repaired_locations"] == ["Kwality House, Kemps Corner"]
    assert "only 0 slots parsed" in output["parse_errors"][0]


def test_ai_repair_uses_single_global_repair_for_multiple_locations(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "rules").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "state" / "03_scores.json").write_text(json.dumps({
        "class_slot_ranking": [],
        "slot_group_ranking": [],
    }))
    (tmp_path / "state" / "02_metrics.json").write_text(json.dumps({
        "trainer_metrics": [],
        "day_band_metrics": [],
    }))
    (tmp_path / "rules" / "trainer_profiles.json").write_text(json.dumps([
        {
            "name": "Trainer A",
            "tier": 1,
            "active": True,
            "qualifications": {"all_barre": True},
            "locations": {},
        },
        {
            "name": "Trainer B",
            "tier": 1,
            "active": True,
            "qualifications": {"all_barre": True},
            "locations": {},
        },
    ]))
    (tmp_path / "config" / "rules_config.json").write_text(json.dumps({
        "categories": {"universal": {"enabled": True}},
        "rules": {},
    }))
    (tmp_path / "config" / "schedule_config.json").write_text(json.dumps({}))

    import agents.ai_planner as ai_planner_module

    locations = ["Kwality House, Kemps Corner", "Supreme HQ, Bandra"]
    global_repair_calls = []

    monkeypatch.setenv("SCHEDULER_FORCE_AI_ONLY", "1")
    monkeypatch.setattr(ai_planner_module, "OPENAI_AVAILABLE", True)
    monkeypatch.setattr(ai_planner_module, "create_ai_client", lambda: (object(), {"model": "test-model"}))
    monkeypatch.setattr(
        AISchedulePlanner,
        "_call_model",
        lambda self, client, model_name, system_prompt, user_prompt, location, max_tokens=None: '{"schedule":[]}',
    )

    def local_conflicting_repair(self, location, scores_data, profiles_by_name):
        return [PlannedSlot(
            location=location,
            date="2026-05-04",
            day_of_week="Monday",
            time="09:00",
            class_name="Studio Barre 57",
            trainer_1="Trainer A",
            trainer_2="",
            cover="",
            room="studio_a",
            capacity=20,
            predicted_fill_rate=0.5,
            score=80.0,
            constraint_violations=[],
        )]

    def global_non_conflicting_repair(self, repair_locations, scores_data, profiles_by_name):
        global_repair_calls.append(tuple(repair_locations))
        return [
            PlannedSlot(
                location=locations[0],
                date="2026-05-04",
                day_of_week="Monday",
                time="09:00",
                class_name="Studio Barre 57",
                trainer_1="Trainer A",
                trainer_2="",
                cover="",
                room="studio_a",
                capacity=20,
                predicted_fill_rate=0.5,
                score=80.0,
                constraint_violations=[],
            ),
            PlannedSlot(
                location=locations[1],
                date="2026-05-04",
                day_of_week="Monday",
                time="09:00",
                class_name="Studio Barre 57",
                trainer_1="Trainer B",
                trainer_2="",
                cover="",
                room="studio_a",
                capacity=20,
                predicted_fill_rate=0.5,
                score=80.0,
                constraint_violations=[],
            ),
        ]

    monkeypatch.setattr(AISchedulePlanner, "_fallback_location", local_conflicting_repair)
    monkeypatch.setattr(AISchedulePlanner, "_fallback_locations", global_non_conflicting_repair, raising=False)

    planner = AISchedulePlanner(target_week_start="2026-05-04", locations=locations)
    output = planner.run()

    assert global_repair_calls == [tuple(locations)]
    assert len(output["schedule"]) == 2
    assert {slot["trainer_1"] for slot in output["schedule"]} == {"Trainer A", "Trainer B"}


def test_deepseek_structural_underfill_skips_free_model_and_repairs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "rules").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "state" / "03_scores.json").write_text(json.dumps({
        "class_slot_ranking": [],
        "slot_group_ranking": [],
    }))
    (tmp_path / "state" / "02_metrics.json").write_text(json.dumps({
        "trainer_metrics": [],
        "day_band_metrics": [],
    }))
    (tmp_path / "rules" / "trainer_profiles.json").write_text(json.dumps([]))
    (tmp_path / "config" / "rules_config.json").write_text(json.dumps({
        "categories": {"universal": {"enabled": True}},
        "rules": {},
    }))
    (tmp_path / "config" / "schedule_config.json").write_text(json.dumps({}))

    import agents.ai_planner as ai_planner_module

    calls = []
    repaired_slot = PlannedSlot(
        location="Kenkere House",
        date="2026-05-04",
        day_of_week="Monday",
        time="09:00",
        class_name="Studio Barre 57",
        trainer_1="Trainer A",
        trainer_2="",
        cover="",
        room="studio_a",
        capacity=20,
        predicted_fill_rate=0.5,
        score=80.0,
        constraint_violations=[],
        rationale="greedy_fallback",
    )

    monkeypatch.setenv("SCHEDULER_FORCE_AI_ONLY", "1")
    monkeypatch.setattr(ai_planner_module, "OPENAI_AVAILABLE", True)
    monkeypatch.setattr(ai_planner_module, "create_ai_client", lambda: (object(), {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com",
    }))
    monkeypatch.setattr(ai_planner_module, "get_ai_fallback_settings", lambda settings: [{
        "provider": "openrouter",
        "model": "openai/gpt-oss-120b:free",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "fallback",
    }])

    def fake_call(self, client, model_name, system_prompt, user_prompt, location, max_tokens=None):
        calls.append(model_name)
        return '{"schedule":[{"day":"Monday","time":"09:00","class":"Studio Barre 57","trainer":"Trainer A"}]}'

    monkeypatch.setattr(AISchedulePlanner, "_call_model", fake_call)
    monkeypatch.setattr(ai_planner_module, "_minimum_ai_slot_count_for_location", lambda location: 3)
    monkeypatch.setattr(ai_planner_module, "_validate_slots", lambda slots, location, profiles: slots)
    monkeypatch.setattr(ai_planner_module, "_enforce_hard_limits", lambda slots, location, profiles: slots[:1])
    monkeypatch.setattr(ai_planner_module, "_score_slots", lambda slots, scores: slots)
    monkeypatch.setattr(ai_planner_module, "_has_enough_slots_after_enforcement", lambda location, slots: False)
    monkeypatch.setattr(
        AISchedulePlanner,
        "_fallback_location",
        lambda self, location, scores_data, profiles_by_name: [repaired_slot],
    )

    planner = AISchedulePlanner(target_week_start="2026-05-04", locations=["Kenkere House"])
    output = planner.run()

    assert calls == ["deepseek-v4-flash"]
    assert output["schedule"][0]["rationale"] == "greedy_fallback"


def test_trainer_hours_mode_prioritizes_underloaded_trainers():
    opt = ScheduleOptimiser(target_week_start="2026-05-04", locations=[], optimization_mode="trainer_hours")
    opt.trainer_states = {
        "Empty Trainer": TrainerState("Empty Trainer", 3),
        "Part Loaded Trainer": TrainerState("Part Loaded Trainer", 2),
        "Loaded Trainer": TrainerState("Loaded Trainer", 1),
    }
    opt.trainer_states["Part Loaded Trainer"].weekly_minutes = 9 * 60
    opt.trainer_states["Loaded Trainer"].weekly_minutes = 14 * 60

    assert opt._trainer_hours_bonus("Part Loaded Trainer", "Monday") > opt._trainer_hours_bonus("Empty Trainer", "Monday") + 25
    assert opt._tier_priority_score("Loaded Trainer") > opt._tier_priority_score("Empty Trainer")


def test_trainer_hours_mode_keeps_quality_score_material():
    opt = ScheduleOptimiser(target_week_start="2026-05-04", locations=[], optimization_mode="trainer_hours")

    proven_slot = opt._apply_optimization_mode_adjustments(
        base_score=88.0,
        shift_bonus=0.0,
        diversity_adjustment=0.0,
        hours_bonus=8.0,
        popularity_bonus=0.0,
        ai_delta=0.0,
        time_penalty=0.0,
        recommendation="INCLUDE",
    )
    weak_utilization_slot = opt._apply_optimization_mode_adjustments(
        base_score=32.0,
        shift_bonus=0.0,
        diversity_adjustment=0.0,
        hours_bonus=95.0,
        popularity_bonus=0.0,
        ai_delta=0.0,
        time_penalty=0.0,
        recommendation="INCLUDE",
    )

    assert proven_slot > weak_utilization_slot


def test_public_score_uses_performance_score_not_placement_score():
    opt = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    score, placement, rec, is_exp = opt._public_score_fields(
        base_score=82.0,
        placement_score=100.0,
        historical_session_count=12,
        recommendation="INCLUDE",
        is_experimental=False,
        manual_pin=False,
    )

    assert score == 82.0
    assert placement == 100.0
    assert rec == "INCLUDE"
    assert not is_exp


def test_zero_history_candidate_is_capped_and_marked_experimental():
    opt = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    score, placement, rec, is_exp = opt._public_score_fields(
        base_score=91.0,
        placement_score=100.0,
        historical_session_count=0,
        recommendation="PROTECT",
        is_experimental=False,
        manual_pin=False,
    )

    assert score == 20.0
    assert placement == 100.0
    assert rec == "EXPERIMENTAL"
    assert is_exp


def test_low_history_candidate_score_caps_by_evidence_band():
    opt = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])

    assert opt._public_score_fields(91.0, 100.0, 2, "INCLUDE", False, False)[0] == 35.0
    assert opt._public_score_fields(91.0, 100.0, 7, "INCLUDE", False, False)[0] == 50.0
    assert opt._public_score_fields(91.0, 100.0, 8, "INCLUDE", False, False)[0] == 91.0


def test_proven_low_performer_history_is_blocked():
    assert is_low_performing_history({
        "session_count": 8,
        "avg_checkin": 1.3,
        "avg_fill_rate": 0.18,
    })
    assert not is_low_performing_history({
        "session_count": 1,
        "avg_checkin": 1.3,
        "avg_fill_rate": 0.18,
    })


def test_early_repair_slots_require_real_history():
    assert not has_early_repair_slot_evidence("Studio FIT", "07:00", {})
    assert not has_early_repair_slot_evidence("Studio Barre 57", "07:45", {
        "session_count": 7,
        "avg_checkin": 12.0,
        "avg_fill_rate": 0.60,
    })
    assert not has_early_repair_slot_evidence("Studio FIT", "08:15", {
        "session_count": 8,
        "avg_checkin": 1.0,
        "avg_fill_rate": 0.10,
    })
    assert has_early_repair_slot_evidence("Studio Barre 57", "07:00", {
        "session_count": 8,
        "avg_checkin": 12.0,
        "avg_fill_rate": 0.60,
    })
    assert has_early_repair_slot_evidence("Studio FIT", "08:45", {})


def test_ai_scoring_flags_low_performing_slots():
    slot = PlannedSlot(
        location="Supreme HQ, Bandra",
        date="2026-05-04",
        day_of_week="Monday",
        time="09:15",
        class_name="Studio Back Body Blaze Express",
        trainer_1="Trainer A",
        trainer_2="",
        cover="",
        room="studio_a",
        capacity=14,
        predicted_fill_rate=0.5,
        score=80,
        constraint_violations=[],
    )
    scores_data = {
        "class_slot_ranking": [{
            "location": "Supreme HQ, Bandra",
            "class": "Studio Back Body Blaze Express",
            "trainer": "Trainer A",
            "day": 0,
            "time": "09:15",
            "score": 35.0,
            "avg_checkin": 1.3,
            "avg_fill_rate": 0.12,
            "session_count": 8,
        }],
        "slot_group_ranking": [],
    }

    scored = _score_slots([slot], scores_data)

    assert scored[0].score == 35.0
    assert any("LOW-PERFORMER" in v for v in scored[0].constraint_violations)


def test_class_variety_mode_rewards_missing_level_at_same_time():
    opt = ScheduleOptimiser(target_week_start="2026-05-04", locations=[], optimization_mode="class_variety")
    opt._time_level_counts = {
        ("Kwality House, Kemps Corner", "09:00", "beginner"): 1,
        ("Kwality House, Kemps Corner", "09:00", "intermediate"): 1,
    }

    advanced = opt._class_level_slot_adjustment("Kwality House, Kemps Corner", "09:00", "Studio FIT")
    beginner_repeat = opt._class_level_slot_adjustment("Kwality House, Kemps Corner", "09:00", "Studio Mat 57 Express")

    assert advanced > 0
    assert beginner_repeat < 0
    assert advanced > beginner_repeat


def test_horizontal_time_mix_blocks_overused_same_class_column():
    opt = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    opt._time_class_counts = {
        ("Kwality House, Kemps Corner", "07:30", "Studio Barre 57"): 4,
    }
    opt._time_format_counts = {
        ("Kwality House, Kemps Corner", "07:30", "barre_family"): 4,
    }

    assert opt._horizontal_mix_allows_candidate(
        "Kwality House, Kemps Corner",
        "07:30",
        "Studio Barre 57",
    ) is False
    assert opt._horizontal_mix_allows_candidate(
        "Kwality House, Kemps Corner",
        "07:30",
        "Studio Mat 57",
    ) is True


def test_horizontal_time_mix_blocks_overused_same_format_column():
    opt = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])
    opt._time_class_counts = {
        ("Kwality House, Kemps Corner", "18:00", "Studio Barre 57"): 2,
        ("Kwality House, Kemps Corner", "18:00", "Studio Cardio Barre"): 2,
    }
    opt._time_format_counts = {
        ("Kwality House, Kemps Corner", "18:00", "barre_family"): 4,
    }

    assert opt._horizontal_mix_allows_candidate(
        "Kwality House, Kemps Corner",
        "18:00",
        "Studio Barre 57",
    ) is False
    assert opt._horizontal_mix_allows_candidate(
        "Kwality House, Kemps Corner",
        "18:00",
        "Studio PowerCycle",
    ) is True


def test_ai_hard_limits_drop_horizontal_same_time_class_overuse():
    slots = [
        PlannedSlot(
            location="Kwality House, Kemps Corner",
            date=f"2026-05-{11 + idx:02d}",
            day_of_week=day,
            time="07:30",
            class_name="Studio Barre 57",
            trainer_1=f"Trainer {idx}",
            trainer_2="",
            cover="",
            room="studio_a",
            capacity=22,
            predicted_fill_rate=0.5,
            score=80.0 - idx,
            constraint_violations=[],
        )
        for idx, day in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], start=1)
    ]
    profiles = {
        f"Trainer {idx}": {
            "active": True,
            "locations": {
                "Kwality House, Kemps Corner": {
                    "available_days": DAY_ORDER,
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 4,
                }
            },
        }
        for idx in range(1, 6)
    }

    kept = _enforce_hard_limits(slots, "Kwality House, Kemps Corner", profiles)

    assert len(kept) == 2
    assert [slot.day_of_week for slot in kept] == ["Monday", "Tuesday"]


def test_ai_hard_limits_cap_daily_minutes_and_single_shift():
    slots = [
        PlannedSlot(
            location="Kwality House, Kemps Corner",
            date="2026-05-11",
            day_of_week="Monday",
            time=time_str,
            class_name="Studio Barre 57",
            trainer_1="Trainer A",
            trainer_2="",
            cover="",
            room=f"studio_{idx}",
            capacity=22,
            predicted_fill_rate=0.5,
            score=90 - idx,
            constraint_violations=[],
        )
        for idx, time_str in enumerate(["07:00", "08:00", "09:00", "10:00", "11:00"], start=1)
    ]
    slots.append(PlannedSlot(
        location="Kwality House, Kemps Corner",
        date="2026-05-11",
        day_of_week="Monday",
        time="18:00",
        class_name="Studio Barre 57",
        trainer_1="Trainer A",
        trainer_2="",
        cover="",
        room="studio_pm",
        capacity=22,
        predicted_fill_rate=0.5,
        score=70,
        constraint_violations=[],
    ))
    profiles = {
        "Trainer A": {
            "tier": 1,
            "active": True,
            "locations": {
                "Kwality House, Kemps Corner": {
                    "available_days": DAY_ORDER,
                    "time_window": {"start": "07:00", "end": "21:00"},
                    "max_classes_per_day": 8,
                }
            },
        }
    }

    kept = _enforce_hard_limits(slots, "Kwality House, Kemps Corner", profiles)

    assert [slot.time for slot in kept] == ["07:00", "08:00", "09:00", "10:00"]


def test_clear_schedule_api_accepts_trailing_slash(tmp_path, monkeypatch):
    monkeypatch.setattr(flask_app_module, "WEB_DIR", tmp_path)
    (tmp_path / "schedule_data.json").write_text(json.dumps({"locations": {"Kenkere House": [{"time": "09:00"}]}}))
    monkeypatch.setattr(flask_app_module, "_save_schedule_to_supabase", lambda data: {"saved": True})

    client = flask_app_module.app.test_client()
    response = client.post("/api/clear-schedule/", json={})

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert json.loads((tmp_path / "schedule_data.json").read_text())["locations"]["Kenkere House"] == []


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
