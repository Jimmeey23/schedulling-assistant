import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).parent
RULES_DIR = PROJECT_ROOT / "rules"
CONFIG_PATH = PROJECT_ROOT / "config" / "rules_config.json"

CATEGORY_META = {
        "universal": {
                "label": "Universal Constraints",
                "description": "Global hard constraints enforced across all three locations — barre mix targets, Sunday rules, consecutive-class limits, and daily capacity caps.",
                "default_enabled": True,
        },
        "format_rules": {
                "label": "Class Format Rules",
                "description": "Per-format scheduling guidance: eligible locations, preferred days/slots, weekly min/max counts, certified-trainer requirements, and mix targets.",
                "default_enabled": True,
        },
        "location_kwality": {
                "label": "Kwality House Rules",
                "description": "Rules specific to Kwality House, Kemps Corner — Strength Lab exclusivity, PowerCycle minimums, and peak-slot ownership.",
                "default_enabled": True,
        },
        "location_supreme": {
                "label": "Supreme HQ Rules",
                "description": "Rules specific to Supreme HQ, Bandra — daily PowerCycle requirements, Recovery placement, and class-mix guardrails.",
                "default_enabled": True,
        },
        "location_kenkere": {
                "label": "Kenkere House Rules",
                "description": "Rules specific to Kenkere House — PowerCycle ban, Barre-family minimums, and trainer ownership blocks.",
                "default_enabled": True,
        },
        "trainer_specific": {
                "label": "Strict Trainer Day Locks",
                "description": "Enforce trainer availability windows as hard constraints. When OFF, availability is treated as a scheduling preference and the AI may deviate for performance reasons.",
                "default_enabled": False,
        },
}

SOURCE_GROUPS = ["universal", "location_kwality", "location_supreme", "location_kenkere"]


def _load_json(path: Path) -> dict:
        with open(path) as f:
                return json.load(f)


def _slugify(value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-")
        return cleaned.upper() or "RULE"


def make_format_rule_id(name: str) -> str:
        return f"FMT-{_slugify(name)}"


def _summarise_format_rule(item: dict) -> str:
        parts: List[str] = []
        family = item.get("family")
        if family:
                parts.append(f"family={family}")
        duration = item.get("duration_min")
        if duration:
                parts.append(f"duration={duration}m")

        eligible = [s for s in (item.get("eligible_locations") or []) if s is not None]
        never_at = [s for s in (item.get("never_at") or []) if s is not None]
        if never_at:
                parts.append("never at " + ", ".join(never_at))
        elif eligible and eligible != ["all"]:
                parts.append("eligible at " + ", ".join(eligible))

        preferred_days = [s for s in (item.get("preferred_days") or []) if s is not None]
        if preferred_days:
                parts.append("preferred days: " + ", ".join(preferred_days))

        preferred_slots = [s for s in (item.get("preferred_slots") or []) if s is not None]
        if preferred_slots:
                parts.append("preferred slots: " + ", ".join(preferred_slots[:6]))

        if item.get("min_per_week"):
                parts.append(f"min/week={item['min_per_week']}")
        if item.get("max_per_week"):
                parts.append(f"max/week={item['max_per_week']}")
        if item.get("target_pct") is not None:
                parts.append(f"target mix={item['target_pct']:.0%}")

        rules = [s for s in (item.get("rules") or []) if s is not None]
        if rules:
                parts.append("rules: " + ", ".join(rules))

        certified = [s for s in (item.get("certified_trainers_only") or []) if s is not None]
        if certified:
                parts.append("certified only: " + ", ".join(certified))

        return "; ".join(parts) or "Format-specific scheduling guidance"


def _rule_command_metadata(rule: dict, enabled: bool = True) -> dict:
        rule_type = rule.get("type", "")
        source = rule.get("source_category", "")
        description = (rule.get("description") or "").lower()

        if not enabled:
                status = "Disabled"
        elif rule_type in {"never_do", "class_location", "trainer_availability"}:
                status = "Recommended"
        elif source == "format_rules":
                status = "Recommended"
        else:
                status = "Recommended"

        if not enabled and rule_type in {"never_do", "class_location", "trainer_availability", "slot_required"}:
                risk = "high"
        elif rule_type in {"never_do", "class_location"}:
                risk = "critical"
        elif rule_type in {"trainer_availability", "slot_required", "always_do"}:
                risk = "high"
        elif rule_type == "class_format":
                risk = "medium"
        else:
                risk = "low"

        if source == "format_rules" or rule_type == "class_format":
                impact = "Class format policy"
        elif rule_type == "trainer_availability":
                impact = "Trainer eligibility"
        elif rule_type == "class_location":
                impact = "Studio placement"
        elif rule_type == "slot_required" or "slot" in description or "time" in description:
                impact = "Slot placement"
        elif "target" in description or "minimum" in description or "maximum" in description:
                impact = "Class count range"
        else:
                impact = "Operational guardrail"

        return {
                "impact_area": impact,
                "risk_level": risk,
                "status_tag": status,
        }


def _build_raw_groups() -> List[dict]:
        universal = _load_json(RULES_DIR / "universal_rules.json")
        kwality = _load_json(RULES_DIR / "kwality_rules.json")
        supreme = _load_json(RULES_DIR / "supreme_rules.json")
        kenkere = _load_json(RULES_DIR / "kenkere_rules.json")
        class_formats = _load_json(RULES_DIR / "class_formats.json")

        source_mapping = [
                ("universal", universal.get("hard_constraints", []), None),
                ("location_kwality", kwality.get("hard_constraints", []), kwality.get("location")),
                ("location_supreme", supreme.get("hard_constraints", []), supreme.get("location")),
                ("location_kenkere", kenkere.get("hard_constraints", []), kenkere.get("location")),
        ]

        groups: List[dict] = []
        all_trainer_rules: List[dict] = []
        for group_id, rules, location in source_mapping:
                group_rules = []
                for rule in rules:
                        entry = {
                                "id": rule["id"],
                                "title": rule["id"],
                                "description": rule["description"],
                                "type": rule.get("type", "always_do"),
                                "location": location,
                                "source_category": group_id,
                                "check_fn_name": rule.get("check_fn_name"),
                        }
                        group_rules.append(entry)
                        if entry["type"] == "trainer_availability":
                                all_trainer_rules.append(deepcopy(entry))

                groups.append({
                        "id": group_id,
                        "label": CATEGORY_META[group_id]["label"],
                        "description": CATEGORY_META[group_id]["description"],
                        "rules": group_rules,
                })

        format_rules = []
        for fmt in class_formats:
                format_rules.append({
                        "id": make_format_rule_id(fmt.get("name", "Unknown Format")),
                        "title": fmt.get("name", "Unknown Format"),
                        "description": _summarise_format_rule(fmt),
                        "type": "class_format",
                        "location": None,
                        "source_category": "format_rules",
                        "format_name": fmt.get("name", "Unknown Format"),
                })

        groups.insert(1, {
                "id": "format_rules",
                "label": CATEGORY_META["format_rules"]["label"],
                "description": CATEGORY_META["format_rules"]["description"],
                "rules": format_rules,
        })
        groups.append({
                "id": "trainer_specific",
                "label": CATEGORY_META["trainer_specific"]["label"],
                "description": CATEGORY_META["trainer_specific"]["description"],
                "rules": all_trainer_rules,
        })
        return groups


def default_rules_config() -> dict:
        groups = _build_raw_groups()
        config = {
                "categories": {
                        category_id: {
                                "enabled": meta["default_enabled"],
                                "label": meta["label"],
                                "description": meta["description"],
                        }
                        for category_id, meta in CATEGORY_META.items()
                },
                "rules": {},
        }
        for group in groups:
                for rule in group.get("rules", []):
                        config["rules"].setdefault(rule["id"], {
                                "enabled": True,
                                "description": rule["description"],
                                "title": rule.get("title", rule["id"]),
                        })
        return config


def _merge_dict(target: dict, source: dict) -> dict:
        for key, value in source.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                        _merge_dict(target[key], value)
                else:
                        target[key] = value
        return target


def load_rules_config() -> dict:
        config = default_rules_config()
        if not CONFIG_PATH.exists():
                return config

        try:
                loaded = _load_json(CONFIG_PATH)
        except Exception:
                return config

        # Legacy flat boolean config support.
        if "categories" not in loaded and "rules" not in loaded:
                for category_id in CATEGORY_META:
                        if category_id in loaded:
                                config["categories"][category_id]["enabled"] = bool(loaded[category_id])
                return config

        if isinstance(loaded.get("categories"), dict):
                for category_id, value in loaded["categories"].items():
                        if category_id not in config["categories"]:
                                continue
                        if isinstance(value, dict):
                                _merge_dict(config["categories"][category_id], value)
                        else:
                                config["categories"][category_id]["enabled"] = bool(value)

        if isinstance(loaded.get("rules"), dict):
                for rule_id, value in loaded["rules"].items():
                        if rule_id not in config["rules"]:
                                config["rules"][rule_id] = {"enabled": True, "description": "", "title": rule_id}
                        if isinstance(value, dict):
                                _merge_dict(config["rules"][rule_id], value)
                        else:
                                config["rules"][rule_id]["enabled"] = bool(value)

        return config


def save_rules_config(config: dict) -> dict:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
                json.dump(config, f, indent=2)
        return config


def update_rules_config(payload: dict) -> dict:
        config = load_rules_config()

        if payload.get("categories"):
                for category_id, value in payload["categories"].items():
                        if category_id not in config["categories"]:
                                continue
                        if isinstance(value, dict):
                                _merge_dict(config["categories"][category_id], value)
                        else:
                                config["categories"][category_id]["enabled"] = bool(value)

        if payload.get("rules"):
                for rule_id, value in payload["rules"].items():
                        if rule_id not in config["rules"]:
                                config["rules"][rule_id] = {"enabled": True, "description": "", "title": rule_id}
                        if isinstance(value, dict):
                                _merge_dict(config["rules"][rule_id], value)
                        else:
                                config["rules"][rule_id]["enabled"] = bool(value)

        return save_rules_config(config)


def build_rules_catalog(config: dict | None = None) -> dict:
        config = config or load_rules_config()
        groups = _build_raw_groups()
        category_payload = {}
        for category_id, meta in CATEGORY_META.items():
                cfg = config["categories"].get(category_id, {})
                category_payload[category_id] = {
                        "id": category_id,
                        "label": cfg.get("label") or meta["label"],
                        "description": cfg.get("description") or meta["description"],
                        "enabled": bool(cfg.get("enabled", meta["default_enabled"])),
                }

        payload_groups = []
        for group in groups:
                category_state = category_payload[group["id"]]
                rules = []
                for rule in group.get("rules", []):
                        override = config["rules"].get(rule["id"], {})
                        merged = deepcopy(rule)
                        merged["title"] = override.get("title") or merged.get("title") or merged["id"]
                        merged["description"] = override.get("description") or merged.get("description") or ""
                        merged["enabled"] = bool(override.get("enabled", True))
                        merged.update(_rule_command_metadata(merged, merged["enabled"]))
                        rules.append(merged)
                payload_groups.append({
                        "id": group["id"],
                        "label": category_state["label"],
                        "description": category_state["description"],
                        "enabled": category_state["enabled"],
                        "rules": rules,
                })

        return {
                "categories": category_payload,
                "groups": payload_groups,
                "config": config,
        }


def get_enabled_rule_ids(config: dict | None = None) -> set[str]:
        config = config or load_rules_config()
        return {
                rule_id
                for rule_id, rule in config.get("rules", {}).items()
                if rule.get("enabled", True)
        }


def get_active_format_rules(config: dict | None = None) -> List[dict]:
        catalog = build_rules_catalog(config)
        group_map = {group["id"]: group for group in catalog["groups"]}
        format_group = group_map.get("format_rules")
        if not format_group or not format_group.get("enabled", False):
                return []
        return [rule for rule in format_group["rules"] if rule.get("enabled", True)]


def get_active_hard_rule_groups(config: dict | None = None) -> Dict[str, List[dict]]:
        catalog = build_rules_catalog(config)
        group_map = {group["id"]: group for group in catalog["groups"]}
        trainer_specific_enabled = catalog["categories"]["trainer_specific"]["enabled"]

        active: Dict[str, List[dict]] = {}
        for group_id in SOURCE_GROUPS:
                group = group_map[group_id]
                if not group.get("enabled", False):
                        active[group_id] = []
                        continue
                group_rules = []
                for rule in group.get("rules", []):
                        if not rule.get("enabled", True):
                                continue
                        if rule.get("type") == "trainer_availability" and not trainer_specific_enabled:
                                continue
                        group_rules.append(rule)
                active[group_id] = group_rules
        return active
