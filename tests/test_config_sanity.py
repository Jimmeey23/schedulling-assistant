import json
from pathlib import Path

from rule_config import build_rules_catalog, default_rules_config


ROOT = Path(__file__).resolve().parents[1]


def test_schedule_targets_never_exceed_daily_maximums():
    config = json.loads((ROOT / "config" / "schedule_config.json").read_text())

    for location, days in config["targets"].items():
        for day, limits in days.items():
            assert limits["target"] <= limits["max"], f"{location} {day}: target exceeds max"


def test_location_weekly_floors_match_policy():
    config = json.loads((ROOT / "config" / "schedule_config.json").read_text())
    expected = {
        "Kwality House, Kemps Corner": 70,
        "Supreme HQ, Bandra": 65,
        "Kenkere House": 55,
    }

    saved_floors = config.get("settings_options", {}).get("location_weekly_floors", {})
    for location, floor in expected.items():
        assert saved_floors.get(location) == floor
        assert sum(day["target"] for day in config["targets"][location].values()) >= floor


def test_static_rules_use_canonical_non_conflicting_language():
    rules_config = json.loads((ROOT / "config" / "rules_config.json").read_text())
    universal_rules = (ROOT / "rules" / "universal_rules.json").read_text().lower()
    claude_rules = (ROOT / "CLAUDE.md").read_text().lower()
    combined = json.dumps(rules_config).lower() + universal_rules + claude_rules

    forbidden_phrases = (
        "sunday max 5-6",
        "exactly 2 full days off",
        "pre/post natal never at any location",
        "atulan purohit exclusively",
        "thursdays only",
    )
    assert not any(phrase in combined for phrase in forbidden_phrases)


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

    assert {"Kwality House, Kemps Corner", "Supreme HQ, Bandra"}.issubset(anisha["locations"])
    assert anisha["locations"]["Kwality House, Kemps Corner"]["available_days"] == [
        "Monday",
        "Tuesday",
        "Wednesday",
    ]
    assert set(anisha["locations"]["Supreme HQ, Bandra"]["available_days"]) == {"Thursday", "Friday"}


def test_trainer_profiles_have_historic_week_off_defaults():
    profiles = json.loads((ROOT / "rules" / "trainer_profiles.json").read_text())
    day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    all_days = set(day_order)

    assert profiles
    for profile in profiles:
        week_offs = profile.get("historic_week_off_days")
        assert isinstance(week_offs, list), profile.get("name")
        assert len(week_offs) <= 2, profile.get("name")
        assert set(week_offs).issubset(all_days), profile.get("name")
        for loc, data in (profile.get("locations") or {}).items():
            days = data.get("available_days") or []
            assert set(days).issubset(all_days), f"{profile.get('name')} {loc}"
            assert not (set(days) == all_days and week_offs), f"{profile.get('name')} {loc}"


def test_tier1_mumbai_trainers_are_available_at_both_main_studios():
    profiles = json.loads((ROOT / "rules" / "trainer_profiles.json").read_text())
    required_locations = {"Kwality House, Kemps Corner", "Supreme HQ, Bandra"}

    for profile in profiles:
        if profile.get("tier") != 1:
            continue
        locations = profile.get("locations") or {}
        if required_locations & set(locations):
            assert required_locations.issubset(locations), profile.get("name")


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
        "atulan",
        "anisha",
        "mrigakshi",
    )
    text = json.dumps(catalog).lower()
    assert not any(term in text for term in forbidden_terms)
