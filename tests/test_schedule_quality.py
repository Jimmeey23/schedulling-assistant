import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.ai_planner import AISchedulePlanner, PlannedSlot, _enforce_hard_limits, _parse_schedule_response, _score_slots
from agents.optimiser import DATA_DRIVEN_DAILY_RANGES, DAY_ORDER, RoomOccupancy, ScheduleOptimiser, ScheduleSlot, TrainerState
from agents.reporter import OutputReporter
from serve import build_pipeline_command, find_available_port


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


def test_schedule_config_targets_override_data_ranges(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "schedule_config.json").write_text(
        '{"targets":{"Kwality House, Kemps Corner":{"Monday":{"target":13,"max":14}}}}'
    )

    optimiser = ScheduleOptimiser(target_week_start="2026-05-04", locations=[])

    assert optimiser._pick_daily_target("Kwality House, Kemps Corner", "Monday") == 13


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


def test_ai_fallback_generates_three_named_optimisation_iterations(monkeypatch):
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
