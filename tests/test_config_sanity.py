import json
from pathlib import Path

from rule_config import build_rules_catalog, default_rules_config


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


def test_anisha_profile_matches_location_specific_rules():
    profiles = json.loads((ROOT / "rules" / "trainer_profiles.json").read_text())
    anisha = next(profile for profile in profiles if profile.get("name") == "Anisha Shah")

    assert anisha["locations"]["Kwality House, Kemps Corner"]["available_days"] == [
        "Monday",
        "Tuesday",
        "Wednesday",
    ]
    assert anisha["locations"]["Supreme HQ, Bandra"]["available_days"] == ["Thursday"]


def test_strength_lab_metadata_does_not_claim_atulan_exclusivity():
    class_formats = json.loads((ROOT / "rules" / "class_formats.json").read_text())
    rules_config = json.loads((ROOT / "config" / "rules_config.json").read_text())

    strength_formats = [
        item for item in class_formats
        if "Strength Lab" in item.get("name", "")
    ]
    assert strength_formats
    assert all("atulan_exclusively" not in (item.get("rules") or []) for item in strength_formats)

    enabled_rule_text = " ".join(
        str(rule.get("description") or "")
        for rule in (rules_config.get("rules") or {}).values()
        if rule.get("enabled") is not False
    ).lower()
    assert "atulan purohit exclusively" not in enabled_rule_text


def test_default_rules_catalog_is_limited_to_universal_rules():
    config = default_rules_config()
    catalog = build_rules_catalog(config)

    assert set(catalog["categories"]) == {"universal"}
    assert [group["id"] for group in catalog["groups"]] == ["universal"]
    assert catalog["groups"][0]["rules"]
    assert all(rule["id"].startswith("UNIV-") for rule in catalog["groups"][0]["rules"])
    forbidden_terms = (
        "kwality",
        "supreme",
        "kenkere",
        "atulan",
        "anisha",
        "mrigakshi",
        "barre 57",
        "powercycle",
        "strength lab",
        "foundations",
        "recovery",
        "pre/post",
    )
    text = json.dumps(catalog).lower()
    assert not any(term in text for term in forbidden_terms)
