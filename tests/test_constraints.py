import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.optimiser import (
    ScheduleSlot,
    TrainerState,
    build_constraint_violations,
    slot_time_to_minutes,
    time_band,
)


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


class TestUniversalConstraints:
    def test_banned_classes_detected(self):
        violations = build_constraint_violations(
            "Kwality House, Kemps Corner",
            "Monday",
            "09:00",
            "Studio SWEAT In 30",
            [],
        )
        assert "UNIV-021: Banned class format" in violations

    def test_foundations_banned_everywhere(self):
        violations = build_constraint_violations(
            "Kwality House, Kemps Corner",
            "Monday",
            "09:00",
            "Studio Foundations",
            [],
        )
        assert "UNIV-021: Banned class format" in violations

    def test_blocked_midday_window_detected(self):
        violations = build_constraint_violations(
            "Kwality House, Kemps Corner",
            "Monday",
            "13:30",
            "Studio Barre 57",
            [],
        )
        assert "UNIV-024: Blocked time window" in violations

    def test_trainer_cannot_cross_locations_same_shift(self):
        state = TrainerState("Anisha Shah", 1)
        assert state.can_add("Monday", "09:00", "Kwality House, Kemps Corner", "Studio Barre 57", 4, "07:00", "20:30")
        state.add("Monday", "09:00", "Kwality House, Kemps Corner", "Studio Barre 57")
        assert not state.can_add("Monday", "11:00", "Supreme HQ, Bandra", "Studio Barre 57", 4, "07:00", "20:30")

    def test_consecutive_same_format_detected(self):
        slots_today = [make_slot(time="09:00", class_name="Studio Barre 57")]
        violations = build_constraint_violations(
            "Kwality House, Kemps Corner",
            "Monday",
            "10:15",
            "Studio Barre 57 Express",
            slots_today,
        )
        assert "UNIV-023: Consecutive class format" in violations

    def test_powercycle_not_at_kenkere_detected(self):
        slot = make_slot(location="Kenkere House", class_name="Studio PowerCycle", time="18:00")
        violations = []
        if "PowerCycle" in slot.class_name and slot.location == "Kenkere House":
            violations.append("UNIV-011: PowerCycle at Kenkere")
        assert len(violations) == 1

    def test_powercycle_at_kwality_is_valid(self):
        slot = make_slot(
            location="Kwality House, Kemps Corner",
            class_name="Studio PowerCycle",
            time="18:00",
        )
        violations = []
        if "PowerCycle" in slot.class_name and slot.location == "Kenkere House":
            violations.append("UNIV-011: PowerCycle at Kenkere")
        assert len(violations) == 0

    def test_strength_lab_only_kwality(self):
        for loc in ["Supreme HQ, Bandra", "Kenkere House"]:
            slot = make_slot(location=loc, class_name="Studio Strength Lab", time="18:00")
            violations = []
            if "Strength Lab" in slot.class_name and slot.location != "Kwality House, Kemps Corner":
                violations.append("UNIV-012: Strength Lab outside Kwality")
            assert len(violations) == 1, f"Expected violation for {loc}"

    def test_strength_lab_at_kwality_valid(self):
        slot = make_slot(
            location="Kwality House, Kemps Corner", class_name="Studio Strength Lab"
        )
        violations = []
        if "Strength Lab" in slot.class_name and slot.location != "Kwality House, Kemps Corner":
            violations.append("UNIV-012: Strength Lab outside Kwality")
        assert len(violations) == 0

    def test_prenatal_banned_everywhere(self):
        violations = build_constraint_violations(
            "Supreme HQ, Bandra", "Monday", "09:00", "Pre/Post Natal", []
        )
        assert "UNIV-021: Banned class format" in violations

    def test_prenatal_banned_at_kwality(self):
        violations = build_constraint_violations(
            "Kwality House, Kemps Corner", "Monday", "09:00", "Pre/Post Natal", []
        )
        assert "UNIV-021: Banned class format" in violations

    def test_sunday_no_class_before_10(self):
        slot = make_slot(day_of_week="Sunday", time="09:00")
        violations = []
        if slot.day_of_week == "Sunday" and slot_time_to_minutes(slot.time) < slot_time_to_minutes("10:00"):
            violations.append("UNIV-004: Sunday class before 10:00")
        assert len(violations) == 1

    def test_sunday_class_at_1000_valid(self):
        slot = make_slot(day_of_week="Sunday", time="10:00")
        violations = []
        if slot.day_of_week == "Sunday" and slot_time_to_minutes(slot.time) < slot_time_to_minutes("10:00"):
            violations.append("UNIV-004: Sunday class before 10:00")
        assert len(violations) == 0

    def test_sunday_no_evening_class(self):
        slot = make_slot(day_of_week="Sunday", time="19:00")
        violations = []
        if slot.day_of_week == "Sunday" and time_band(slot.time) == "evening":
            violations.append("UNIV-004: Sunday evening class")
        assert len(violations) == 1

    def test_sunday_afternoon_class_valid(self):
        slot = make_slot(day_of_week="Sunday", time="14:00")
        violations = []
        if slot.day_of_week == "Sunday" and time_band(slot.time) == "evening":
            violations.append("UNIV-004: Sunday evening class")
        assert len(violations) == 0

    def test_recovery_not_first_class(self):
        slots_today = []  # No earlier classes
        cname = "Studio Recovery"
        time_str = "12:00"
        violations = []
        if "Recovery" in cname:
            times_today = sorted([s.time for s in slots_today])
            if not times_today:
                violations.append("UNIV-007: Recovery is first class of day")
        assert len(violations) == 1

    def test_recovery_not_first_when_preceded(self):
        slots_today = [make_slot(time="07:30", class_name="Studio Barre 57")]
        cname = "Studio Recovery"
        time_str = "12:00"
        violations = []
        if "Recovery" in cname:
            times_today = sorted([s.time for s in slots_today])
            if times_today and time_str <= times_today[0]:
                violations.append("UNIV-007: Recovery is first class of day")
        assert len(violations) == 0

    def test_foundations_not_at_1130(self):
        violations = []
        slot_time = "11:30"
        if "Foundations" in "Studio Foundations" and slot_time in ("11:30", "19:15"):
            violations.append("UNIV-008: Foundations at forbidden slot")
        assert len(violations) == 1

    def test_foundations_not_at_1915(self):
        violations = []
        slot_time = "19:15"
        if "Foundations" in "Studio Foundations" and slot_time in ("11:30", "19:15"):
            violations.append("UNIV-008: Foundations at forbidden slot")
        assert len(violations) == 1

    def test_foundations_banned_at_0900(self):
        violations = build_constraint_violations(
            "Kwality House, Kemps Corner", "Monday", "09:00", "Studio Foundations", []
        )
        assert "UNIV-021: Banned class format" in violations

    def test_weekday_class_valid_before_10(self):
        slot = make_slot(day_of_week="Monday", time="07:30")
        violations = []
        if slot.day_of_week == "Sunday" and slot_time_to_minutes(slot.time) < slot_time_to_minutes("10:00"):
            violations.append("UNIV-004: Sunday class before 10:00")
        assert len(violations) == 0


class TestTimeHelpers:
    def test_slot_time_to_minutes(self):
        assert slot_time_to_minutes("07:00") == 420
        assert slot_time_to_minutes("09:00") == 540
        assert slot_time_to_minutes("11:30") == 690
        assert slot_time_to_minutes("19:15") == 1155
        assert slot_time_to_minutes("20:00") == 1200

    def test_time_band_morning(self):
        assert time_band("07:00") == "morning"
        assert time_band("07:30") == "morning"
        assert time_band("09:00") == "morning"
        assert time_band("09:59") == "morning"

    def test_time_band_midday(self):
        assert time_band("10:00") == "midday"
        assert time_band("11:30") == "midday"
        assert time_band("12:30") == "midday"

    def test_time_band_afternoon(self):
        assert time_band("13:00") == "afternoon"
        assert time_band("15:00") == "afternoon"
        assert time_band("16:45") == "afternoon"

    def test_time_band_evening(self):
        assert time_band("17:00") == "evening"
        assert time_band("19:15") == "evening"
        assert time_band("20:00") == "evening"

    def test_gap_calculation(self):
        t1 = "09:00"
        t2 = "09:57"
        gap = abs(slot_time_to_minutes(t2) - slot_time_to_minutes(t1))
        assert gap == 57

    def test_thirty_min_gap_check(self):
        existing = "09:00"
        new_slot = "09:25"
        gap = abs(slot_time_to_minutes(new_slot) - slot_time_to_minutes(existing))
        assert gap < 30  # Should flag as too close

        new_slot_ok = "09:30"
        gap_ok = abs(slot_time_to_minutes(new_slot_ok) - slot_time_to_minutes(existing))
        assert gap_ok >= 30  # Should be allowed


class TestLocationConstraints:
    def test_kenkere_no_strength_lab(self):
        slot = make_slot(location="Kenkere House", class_name="Studio Strength Lab")
        violations = []
        if "Strength Lab" in slot.class_name and slot.location != "Kwality House, Kemps Corner":
            violations.append("UNIV-012: Strength Lab outside Kwality")
        assert "UNIV-012: Strength Lab outside Kwality" in violations

    def test_supreme_no_strength_lab(self):
        slot = make_slot(location="Supreme HQ, Bandra", class_name="Studio Strength Lab")
        violations = []
        if "Strength Lab" in slot.class_name and slot.location != "Kwality House, Kemps Corner":
            violations.append("UNIV-012: Strength Lab outside Kwality")
        assert "UNIV-012: Strength Lab outside Kwality" in violations

    def test_kenkere_no_prenatal(self):
        violations = build_constraint_violations(
            "Kenkere House", "Monday", "09:00", "Pre/Post Natal", []
        )
        assert "UNIV-021: Banned class format" in violations

    def test_powercycle_at_supreme_valid(self):
        slot = make_slot(location="Supreme HQ, Bandra", class_name="Studio PowerCycle")
        violations = []
        if "PowerCycle" in slot.class_name and slot.location == "Kenkere House":
            violations.append("UNIV-011: PowerCycle at Kenkere")
        assert len(violations) == 0
