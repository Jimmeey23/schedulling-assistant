import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_schedule_targets_never_exceed_daily_maximums():
    config = json.loads((ROOT / "config" / "schedule_config.json").read_text())

    for location, days in config["targets"].items():
        for day, limits in days.items():
            assert limits["target"] <= limits["max"], f"{location} {day}: target exceeds max"


def test_strength_lab_weekly_targets_are_consistent():
    config = json.loads((ROOT / "config" / "schedule_config.json").read_text())
    kwality_mix = config["class_mix"]["Kwality House, Kemps Corner"]
    strength_lab_target = kwality_mix["Studio Strength Lab"]

    assert strength_lab_target["max"] <= 4


def test_persisted_trainer_profiles_have_real_qualifications():
    profiles = json.loads((ROOT / "rules" / "trainer_profiles.json").read_text())

    assert any(
        any(profile.get("qualifications", {}).values())
        for profile in profiles
    )
