"""
Agent 5 — AI Schedule Planner
An OpenRouter/OpenAI-compatible model reads all historical data, scores,
constraints, and trainer profiles, then actively builds the complete weekly
schedule for each location.
Falls back to the greedy ScheduleOptimiser only if no AI API key is available.
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from agents.draft_retention import prune_draft_schedule_files
from agents.io_utils import atomic_write_json
from agents.optimiser import (
    HORIZONTAL_MAX_SAME_CLASS_PER_TIME,
    HORIZONTAL_MAX_SAME_FORMAT_PER_TIME,
    LOCATION_ROOMS,
    MAX_TRAINER_WEEKLY_MINUTES_T1,
    MAX_TRAINER_WEEKLY_MINUTES_T2,
    MAX_TRAINER_WEEKLY_MINUTES_T3,
    MAX_TRAINER_WEEKLY_MINUTES,
    RoomOccupancy,
    canonical_class_key,
    get_class_format,
    get_class_duration,
    slot_time_to_minutes,
    time_windows_overlap,
)
from ai_provider import OPENAI_AVAILABLE, create_ai_client, create_chat_completion, get_ai_settings
from rule_config import build_rules_catalog, get_active_format_rules, load_rules_config

STATE_DIR = Path("state")
RULES_DIR = Path("rules")
CONFIG_DIR = Path("config")

MAX_TOKENS = 12000  # Supreme needs ~9k for 68 verbose slots; others ~3.5k

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DOW_INT = {d: i for i, d in enumerate(DAY_NAMES)}


def normalize_trainer_name(name: str) -> str:
    return " ".join(str(name or "").split()).strip()

# Historical data-driven daily class count targets
DAILY_TARGETS = {
    "Kwality House, Kemps Corner": {
        "Monday": 12, "Tuesday": 10, "Wednesday": 11,
        "Thursday": 11, "Friday": 10, "Saturday": 12, "Sunday": 6,
    },
    "Supreme HQ, Bandra": {
        "Monday": 12, "Tuesday": 11, "Wednesday": 10,
        "Thursday": 12, "Friday": 11, "Saturday": 12, "Sunday": 6,
    },
    "Kenkere House": {
        "Monday": 7, "Tuesday": 7, "Wednesday": 7,
        "Thursday": 7, "Friday": 6, "Saturday": 8, "Sunday": 5,
    },
}

# Available time slots per location (AM then PM)
LOCATION_SLOTS = {
    "Kwality House, Kemps Corner": {
        "am": ["07:15", "07:30", "08:00", "08:15", "08:30", "08:45",
               "09:00", "09:15", "09:30", "10:00", "10:15", "11:00", "11:15", "11:30"],
        "pm": ["17:00", "17:15", "17:30", "17:45", "18:00", "18:15",
               "18:30", "18:45", "19:00", "19:15", "19:30"],
    },
    "Supreme HQ, Bandra": {
        "am": ["07:30", "08:00", "08:15", "08:30", "08:45", "09:00",
               "09:15", "09:30", "09:45", "10:00", "10:15", "10:30",
               "11:00", "11:30", "12:00", "12:30"],
        "pm": ["16:30", "17:00", "17:30", "17:45", "18:00", "18:15",
               "18:30", "19:00", "19:15", "19:30", "20:00", "20:15"],
    },
    "Kenkere House": {
        "am": ["07:15", "08:30", "09:00", "09:15", "10:00", "10:15",
               "11:00", "11:15", "11:30", "11:45", "12:30"],
        "pm": ["17:00", "17:15", "18:00", "18:15", "18:30", "19:15", "19:30"],
    },
}


@dataclass
class PlannedSlot:
    location: str
    date: str
    day_of_week: str
    time: str
    class_name: str
    trainer_1: str
    trainer_2: str
    cover: str
    room: str
    capacity: int
    predicted_fill_rate: float
    score: float
    constraint_violations: List[str]
    duration_min: int = 57
    rationale: str = ""


# ---------------------------------------------------------------------------
# System prompt — comprehensive rules, trainer profiles, class formats
# ---------------------------------------------------------------------------

def _build_system_prompt(profiles: list, rules_catalog: dict) -> str:
    category_map = rules_catalog["categories"]
    group_map = {group["id"]: group for group in rules_catalog["groups"]}
    trainer_specific_on = bool(category_map.get("trainer_specific", {}).get("enabled", False))

    trainer_lines = []
    for p in sorted(profiles, key=lambda x: (x.get("tier", 3), x["name"])):
        name = p["name"]
        tier = p.get("tier", 3)
        quals = p.get("qualifications", {})
        qual_list = [k for k, v in quals.items() if v and k]
        locs_text = []
        for loc, ld in p.get("locations", {}).items():
            loc_abbr = "Kwality" if "Kwality" in loc else ("Supreme" if "Supreme" in loc else "Kenkere")
            days = ", ".join(d[:3] for d in (ld.get("available_days") or []) if d)
            tw = ld.get("time_window") or {}
            start, end = tw.get("start", "07:00"), tw.get("end", "20:00")
            max_day = ld.get("max_classes_per_day") or 3
            avg_ci = ld.get("avg_checkin") or 0
            sessions = ld.get("session_count") or 0
            owned = ld.get("owned_blocks") or []
            owned_str = ""
            if trainer_specific_on and owned:
                owned_str = " | OWNS: " + "; ".join(
                    f"{o.get('day', '')} {','.join(t for t in (o.get('times') or []) if t)}"
                    for o in owned
                )
            notes = ld.get("notes", "")
            # When trainer_specific is OFF, frame availability as preference not hard rule
            if trainer_specific_on:
                avail_label = f"days={days}"
            else:
                avail_label = f"typically={days}"
            locs_text.append(
                f"    {loc_abbr}: {avail_label} window={start}-{end} max={max_day}/day "
                f"avgCI={avg_ci:.1f} n={sessions}{owned_str}"
                + (f"\n    NOTE: {notes}" if notes else "")
            )
        trainer_lines.append(
            f"[T{tier}] {name} | quals: {', '.join(qual_list)}\n" + "\n".join(locs_text)
        )

    fmt_lines = [
        f"  - {rule['title']}: {rule['description']}"
        for rule in get_active_format_rules(rules_catalog.get('config'))
    ]

    def render_rule_block(group_id: str, heading: str) -> str:
        group = group_map.get(group_id)
        if not group or not group.get("enabled", False):
            return ""
        rules = [rule for rule in group.get("rules", []) if rule.get("enabled", True)]
        if not rules:
            return ""
        lines = [heading]
        lines.extend(f"  - {rule['id']}: {rule['description']}" for rule in rules)
        return "\n".join(lines)

    if trainer_specific_on:
        trainer_day_note = "TRAINER DAYS ARE HARD RULES — schedule trainers ONLY on their listed days."
    else:
        trainer_day_note = ("TRAINER DAYS are typical availability for scheduling preference only "
                            "— not hard restrictions. Use best-fit based on performance data.")

    universal_block = render_rule_block("universal", "HARD CONSTRAINTS — ALL LOCATIONS:")

    formats_block = ""
    if category_map.get("format_rules", {}).get("enabled", False) and fmt_lines:
        formats_block = "FORMATS:\n" + "\n".join(fmt_lines)

    sections = [
        "Physique 57 India head scheduler. Build complete 7-day schedules from historical data + rules.",
        "",
        f"TRAINERS (T1=star, never compromise peak slots):\n{trainer_day_note}",
        chr(10).join(trainer_lines),
    ]
    if formats_block:
        sections.append(formats_block)
    if universal_block:
        sections.append(universal_block)
    sections.append(
        "Use only universal defaults plus rules saved in Settings. Do not invent trainer-, studio-, or class-specific policies."
    )
    sections.append(
        "Horizontal weekly mix: for each location and clock time, vary class formats across the week. "
        "Use no more than 2 exact same classes and no more than 3 same-format classes at any one clock time. "
        "Do not fill a time column with the same class or same format every day."
    )
    sections.append("Output ONLY raw JSON, no markdown, no extra text.")

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Per-location user prompt — historical data + targets + format spec
# ---------------------------------------------------------------------------

def _build_location_prompt(location: str, week_start: str,
                            scores_data: dict, metrics_data: dict,
                            profiles: list = None) -> str:
    from datetime import date, timedelta
    week_date = date.fromisoformat(week_start)
    day_to_date = {d: (week_date + timedelta(days=i)).isoformat() for i, d in enumerate(DAY_NAMES)}

    ranking = scores_data.get("class_slot_ranking", [])
    trainer_metrics = metrics_data.get("trainer_metrics", [])
    day_band = metrics_data.get("day_band_metrics", [])
    profiles_by_name = {p.get("name"): p for p in (profiles or []) if p.get("name")}
    disabled_trainers = _disabled_trainer_names(profiles_by_name)

    # Top performers for this location (fill ≥ 28%, trainer ran ≥ 5 sessions)
    # Sort by blended score first, then by recency-boosted score
    loc_top = [
        r for r in ranking
        if r["location"] == location
        and normalize_trainer_name(r.get("trainer")) not in disabled_trainers
        and r.get("avg_fill_rate", 0) >= 0.28
        and (r.get("trainer_total_sessions") or r.get("session_count", 0)) >= 5
    ]
    loc_top.sort(key=lambda x: (-x["score"], -x.get("avg_fill_rate", 0)))

    # Bottom combos to avoid (fill < 22%, trainer ran ≥ 5 sessions)
    loc_avoid = [
        r for r in ranking
        if r["location"] == location
        and normalize_trainer_name(r.get("trainer")) not in disabled_trainers
        and r.get("avg_fill_rate", 0) < 0.22
        and (r.get("trainer_total_sessions") or r.get("session_count", 0)) >= 5
    ]
    loc_avoid.sort(key=lambda x: x.get("avg_fill_rate", 0))

    # Trainer performance at this location
    loc_trainers = [t for t in trainer_metrics if t["location"] == location]
    loc_trainers.sort(key=lambda x: -x.get("trainer_avg_checkin", 0))

    targets = DAILY_TARGETS.get(location, {d: 7 for d in DAY_NAMES}).copy()
    schedule_config_path = CONFIG_DIR / "schedule_config.json"
    if schedule_config_path.exists():
        try:
            with open(schedule_config_path) as f:
                schedule_config = json.load(f)
            configured_targets = schedule_config.get("targets", {}).get(location, {})
            for day in DAY_NAMES:
                day_limits = configured_targets.get(day)
                if isinstance(day_limits, dict) and day_limits.get("target") is not None:
                    targets[day] = int(day_limits["target"])
        except Exception:
            pass
    slots = LOCATION_SLOTS.get(location, {"am": [], "pm": []})

    lines = [
        f"## Build schedule for: {location}",
        f"Week: {week_start} — day→date mapping:",
    ]
    for day in DAY_NAMES:
        lines.append(f"  {day}: {day_to_date[day]}")

    lines += ["", "### Target class counts (hit exactly):"]
    total_classes = sum(targets.values())
    for day in DAY_NAMES:
        lines.append(f"  {day}: {targets.get(day, 7)}")
    lines.append(f"  WEEK TOTAL: {total_classes}")

    lines += ["", "### Available time slots:"]
    lines.append(f"  AM: {', '.join(slots['am'])}")
    lines.append(f"  PM: {', '.join(slots['pm'])}")

    # Trainer availability — with hard/soft distinction
    if profiles:
        rules_config = load_rules_config()
        trainer_specific_on = bool(rules_config.get("categories", {}).get("trainer_specific", {}).get("enabled", False))
        avail_header = (
            "### TRAINER AVAILABILITY — HARD LOCKS (only schedule on listed days):"
            if trainer_specific_on else
            "### TRAINER AVAILABILITY — default preference (respect unless strong historical reason):"
        )
        lines += ["", avail_header]
        for p in profiles:
            if normalize_trainer_name(p.get("name")) in disabled_trainers:
                continue
            loc_data = p.get("locations", {}).get(location)
            if not loc_data:
                continue
            avail = loc_data.get("available_days") or []
            if not avail:
                continue
            tier = p.get("tier", 3)
            days_str = "/".join(d[:3] for d in avail)
            tw = loc_data.get("time_window") or {}
            window = f"{tw.get('start','07:00')}-{tw.get('end','20:00')}"
            max_d = loc_data.get("max_classes_per_day") or 3
            owned = loc_data.get("owned_blocks") or []
            owned_str = ""
            if owned:
                owned_str = " | OWNS: " + "; ".join(
                    f"{o.get('day','')[:3]} {','.join(t for t in (o.get('times') or []) if t)}"
                    for o in owned
                )
            lines.append(f"  [T{tier}] {p['name']}: {days_str} | window={window} | max={max_d}/day{owned_str}")

    # Top 30 performers with recency signal
    lines += ["", "### TOP PERFORMERS — class|trainer|day@time|fill|score|recency_boost|n:"]
    for r in loc_top[:30]:
        day_name = DAY_NAMES[r["day"]] if isinstance(r.get("day"), int) and 0 <= r["day"] <= 6 else r.get("day_name", "?")
        n = r.get("trainer_total_sessions") or r.get("session_count", 0)
        rb = r.get("recency_boost", 0.0)
        rb_str = f"+{rb:.1f}" if rb > 0 else (f"{rb:.1f}" if rb < 0 else "0")
        lines.append(
            f"  {r['class']}|{r['trainer']}|{day_name}@{r['time']}|"
            f"{r['avg_fill_rate']:.0%}|{r['score']:.0f}|{rb_str}|{n}"
        )

    # Avoid combos
    if loc_avoid:
        lines += ["", "### AVOID (fill<22%, ≥5 sessions):"]
        for r in loc_avoid[:12]:
            day_name = DAY_NAMES[r["day"]] if isinstance(r.get("day"), int) and 0 <= r["day"] <= 6 else r.get("day_name", "?")
            lines.append(f"  {r['class']}|{r['trainer']}|{day_name}@{r['time']}|{r['avg_fill_rate']:.0%}")

    lines += [
        "",
        "Return JSON only — no markdown, no explanation.",
        'Schema: {"location":"...","week_start":"...","schedule":[{"day":"Monday","time":"08:30","class":"Studio Barre 57","trainer":"Trainer Name","cover":"Cover Trainer"},...]}',
        "",
        "CRITICAL: ALL 7 days. Hit saved daily targets. Use exact class/trainer names from above. No duplicate times per day. Every slot needs a cover trainer. Apply only universal defaults plus rules saved in Settings.",
        "SUPREME 08:00-09:00 FOCUS: for Supreme HQ, do not collapse the 8-9am demand window into only 09:00. Use the available 08:30 and 08:45 starts where trainer/room constraints allow.",
        "HORIZONTAL MIX: At the same clock time across the week, rotate formats/classes. Keep each exact class to 2 or fewer uses per clock time, each broad format to 3 or fewer uses per clock time, and do not make 07:30/08:30/09:00 all Barre 57, all PowerCycle, or any single repeated format.",
        "TIER UTILIZATION: Ensure Tier 1 (T1) trainers are scheduled for more total hours/classes than Tier 2 (T2) trainers, and Tier 2 trainers are scheduled for more than Tier 3 (T3) trainers across the location. Maximize T1 trainer utilization first. Every trainer MUST get exactly 2 days off per week.",
        "LOCATION BALANCE: For trainers available at multiple locations (like Rohan, Karanvir, Richard), do not park them at only one location. Spread their sessions across their available locations to ensure a consistent brand presence.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> Optional[str]:
    """Extract a JSON object from the model response, handling fenced output."""
    text = raw.strip()

    # Strip markdown fences
    if "```" in text:
        for part in text.split("```")[1:]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break

    # Find outermost JSON object
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_schedule_response(raw: str, location: str, week_start: str,
                              profiles_by_name: dict) -> Tuple[List[PlannedSlot], List[str]]:
    from datetime import date, timedelta

    json_str = _extract_json(raw)
    if not json_str:
        return [], ["No JSON object found in response"]

    # Fix trailing commas
    json_str = re.sub(r",\s*([}\]])", r"\1", json_str)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        return [], [f"JSON parse error: {e}"]

    slots = []
    errors = []
    week_date = date.fromisoformat(week_start)
    day_to_date = {d: (week_date + timedelta(days=i)).isoformat() for i, d in enumerate(DAY_NAMES)}
    seen_slots: Set[Tuple[str, str, str, str]] = set()

    for entry in data.get("schedule", []):
        day = entry.get("day", "")
        time_str = entry.get("time", "")
        class_name = entry.get("class", "")
        trainer = entry.get("trainer", "")
        cover = entry.get("cover", "")
        rationale = entry.get("rationale", "")

        if day not in DAY_NAMES:
            errors.append(f"Unknown day: {day!r}")
            continue
        if not time_str or not class_name or not trainer:
            errors.append(f"Incomplete slot: {entry}")
            continue
        if _trainer_disabled(trainer, profiles_by_name):
            errors.append(f"Disabled trainer skipped: {trainer} at {day} {time_str}")
            continue

        # Normalize HH:MM
        time_str = time_str.strip()
        if len(time_str) == 4 and time_str[1] == ":":
            time_str = "0" + time_str

        slot_key = (day, time_str, class_name, normalize_trainer_name(trainer))
        if slot_key in seen_slots:
            errors.append(f"Duplicate slot {class_name} with {trainer} at {time_str} on {day} — keeping first")
            continue
        seen_slots.add(slot_key)

        slots.append(PlannedSlot(
            location=location,
            date=day_to_date.get(day, ""),
            day_of_week=day,
            time=time_str,
            class_name=class_name,
            trainer_1=trainer,
            trainer_2="",
            cover=cover,
            room="",
            capacity=15,
            predicted_fill_rate=0.4,
            score=0.0,
            constraint_violations=[],
            duration_min=get_class_duration(class_name),
            rationale=rationale,
        ))

    _assign_rooms(slots, location)
    return slots, errors


def _class_family_for_room(class_name: str) -> str:
    lower = str(class_name or "").lower()
    if "powercycle" in lower or "power cycle" in lower:
        return "powercycle"
    if "strength lab" in lower:
        return "strength_lab"
    return "barre_57"


def _assign_rooms(slots: List[PlannedSlot], location: str) -> None:
    rooms = LOCATION_ROOMS.get(location, {})
    if not rooms:
        return
    occupancy = RoomOccupancy(rooms)
    for slot in sorted(slots, key=lambda s: (DAY_NAMES.index(s.day_of_week), s.time, s.class_name)):
        try:
            start_min = slot_time_to_minutes(slot.time)
        except Exception:
            slot.constraint_violations.append("UNIV-025: Invalid time for room assignment")
            continue
        family = _class_family_for_room(slot.class_name)
        room = occupancy.find_room(slot.day_of_week, family, start_min, slot.duration_min)
        if room is None:
            slot.constraint_violations.append("UNIV-025: No available room for class duration")
            continue
        slot.room = room
        slot.capacity = int(rooms.get(room, {}).get("capacity", slot.capacity))
        occupancy.occupy(slot.day_of_week, room, start_min, slot.duration_min, slot.class_name, slot.trainer_1)


# ---------------------------------------------------------------------------
# Post-processing: validate + score
# ---------------------------------------------------------------------------

def _validate_slots(slots: List[PlannedSlot], location: str,
                    profiles_by_name: dict) -> List[PlannedSlot]:
    from collections import defaultdict
    day_trainer_count: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for slot in slots:
        day_trainer_count[slot.day_of_week][slot.trainer_1] += 1

    for slot in slots:
        v = slot.constraint_violations
        prof = profiles_by_name.get(slot.trainer_1, {})
        if _trainer_disabled(slot.trainer_1, profiles_by_name):
            v.append(f"UNIV-000: {slot.trainer_1} is inactive/disabled and cannot be scheduled")
        loc_data = prof.get("locations", {}).get(location, {})
        avail_days = loc_data.get("available_days", [])
        if avail_days and slot.day_of_week not in avail_days:
            v.append(f"UNIV-020: {slot.trainer_1} not available at {location} on {slot.day_of_week}")

        if slot.day_of_week == "Sunday":
            h, m = map(int, slot.time.split(":"))
            if h < 10:
                v.append("UNIV-004: No Sunday class before 10:00")

        max_day = loc_data.get("max_classes_per_day", 4)
        if day_trainer_count[slot.day_of_week][slot.trainer_1] > max_day:
            v.append(f"UNIV-010: {slot.trainer_1} exceeds daily limit ({max_day}) on {slot.day_of_week}")

    return slots


def _disabled_trainer_names(profiles_by_name: dict) -> Set[str]:
    disabled = {
        normalize_trainer_name(name)
        for name, profile in (profiles_by_name or {}).items()
        if profile.get("active") is False
    }
    for path in (CONFIG_DIR / "trainer_overrides.json", CONFIG_DIR / "schedule_config.json"):
        if not path.exists():
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            disabled.update(normalize_trainer_name(t) for t in data.get("inactive_trainers", []))
        except Exception:
            continue
    return disabled


def _trainer_disabled(trainer: str, profiles_by_name: dict) -> bool:
    return normalize_trainer_name(trainer) in _disabled_trainer_names(profiles_by_name)


def _enforce_hard_limits(slots: List[PlannedSlot], location: str,
                         profiles_by_name: dict = None) -> List[PlannedSlot]:
    """Drop slots violating hard weekly caps, day/time rules, and trainer availability."""
    from collections import defaultdict

    # Build trainer availability lookup
    trainer_allowed_days: Dict[str, set] = {}
    if profiles_by_name:
        for name, prof in profiles_by_name.items():
            if _trainer_disabled(name, profiles_by_name):
                continue
            loc_data = prof.get("locations", {}).get(location, {})
            avail = loc_data.get("available_days")
            if avail:
                trainer_allowed_days[name] = set(avail)

    normalized_profiles = {
        normalize_trainer_name(name): profile
        for name, profile in (profiles_by_name or {}).items()
    }

    def _profile_for(trainer: str) -> dict:
        return (profiles_by_name or {}).get(trainer) or normalized_profiles.get(normalize_trainer_name(trainer), {})

    def _time_window_allows(slot: PlannedSlot, loc_data: dict) -> bool:
        window = loc_data.get("time_window") or {}
        start = window.get("start", "06:00")
        end = window.get("end", "22:00")
        start_min = slot_time_to_minutes(slot.time)
        return slot_time_to_minutes(start) <= start_min < slot_time_to_minutes(end)

    def _weekly_mix_max(class_name: str) -> Optional[int]:
        config_path = CONFIG_DIR / "schedule_config.json"
        if not config_path.exists():
            return None
        try:
            with open(config_path) as f:
                config = json.load(f)
            mix = config.get("class_mix", {}).get(location, {})
            canonical = canonical_class_key(class_name)
            entry = mix.get(class_name) or mix.get(canonical) or {}
            if isinstance(entry, dict) and entry.get("max") is not None:
                return int(entry["max"])
        except Exception:
            return None
        return None

    kept = []
    class_weekly_counts: Dict[str, int] = defaultdict(int)
    time_class_counts: Dict[Tuple[str, str, str], int] = defaultdict(int)
    time_format_counts: Dict[Tuple[str, str, str], int] = defaultdict(int)
    trainer_day_slots: Dict[Tuple[str, str], List[PlannedSlot]] = defaultdict(list)
    trainer_day_location_count: Dict[Tuple[str, str, str], int] = defaultdict(int)
    trainer_weekly_minutes: Dict[str, int] = defaultdict(int)
    trainer_worked_days: Dict[str, Set[str]] = defaultdict(set)

    for slot in sorted(slots, key=lambda s: (DAY_NAMES.index(s.day_of_week), s.time, s.class_name)):
        if _trainer_disabled(slot.trainer_1, profiles_by_name or {}):
            continue
        existing_violations = slot.constraint_violations or []
        if any(
            marker in violation
            for violation in existing_violations
            for marker in ("Invalid time", "No available room")
        ):
            continue
        trainer_key = normalize_trainer_name(slot.trainer_1)
        profile = _profile_for(slot.trainer_1)
        loc_data = (profile.get("locations") or {}).get(location, {})
        class_key = canonical_class_key(slot.class_name)
        weekly_mix_max = _weekly_mix_max(slot.class_name)
        if weekly_mix_max is not None and class_weekly_counts[class_key] >= weekly_mix_max:
            continue
        time_class_key = (location, slot.time, canonical_class_key(slot.class_name))
        if time_class_counts[time_class_key] >= HORIZONTAL_MAX_SAME_CLASS_PER_TIME:
            continue
        time_format_key = (location, slot.time, get_class_format(slot.class_name))
        if time_format_counts[time_format_key] >= HORIZONTAL_MAX_SAME_FORMAT_PER_TIME:
            continue
        # Sunday: no evening band (17:00+), no class before 10:00
        if slot.day_of_week == "Sunday":
            h = int(slot.time[:2])
            if h >= 17 or h < 10:
                continue
        # Trainer must be available on that day
        allowed = trainer_allowed_days.get(slot.trainer_1) or trainer_allowed_days.get(trainer_key)
        if allowed and slot.day_of_week not in allowed:
            continue
        if loc_data and not _time_window_allows(slot, loc_data):
            continue
        max_day = int(loc_data.get("max_classes_per_day") or 4)
        day_loc_key = (trainer_key, slot.day_of_week, location)
        if trainer_day_location_count[day_loc_key] >= max_day:
            continue
        duration = int(slot.duration_min or get_class_duration(slot.class_name))
        tier = profile.get("tier", 3)
        max_mins = MAX_TRAINER_WEEKLY_MINUTES_T1 if tier == 1 else (MAX_TRAINER_WEEKLY_MINUTES_T2 if tier == 2 else MAX_TRAINER_WEEKLY_MINUTES_T3)
        if trainer_weekly_minutes[trainer_key] + duration > max_mins:
            continue
        if (
            slot.day_of_week not in trainer_worked_days[trainer_key]
            and len(trainer_worked_days[trainer_key]) >= 5
        ):
            continue
        day_key = (trainer_key, slot.day_of_week)
        start_min = slot_time_to_minutes(slot.time)
        if any(
            time_windows_overlap(
                start_min,
                duration,
                slot_time_to_minutes(existing.time),
                int(existing.duration_min or get_class_duration(existing.class_name)),
            )
            for existing in trainer_day_slots[day_key]
        ):
            continue
        kept.append(slot)
        class_weekly_counts[class_key] += 1
        time_class_counts[time_class_key] += 1
        time_format_counts[time_format_key] += 1
        trainer_day_slots[day_key].append(slot)
        trainer_day_location_count[day_loc_key] += 1
        trainer_weekly_minutes[trainer_key] += duration
        trainer_worked_days[trainer_key].add(slot.day_of_week)

    def _shift_key(time_str: str) -> int:
        h = int(time_str[:2])
        return 0 if h < 12 else (1 if h < 17 else 2)

    day_slots: Dict[str, list] = defaultdict(list)
    for s in kept:
        day_slots[s.day_of_week].append(s)

    final = []
    for day, day_list in day_slots.items():
        by_shift: Dict[int, list] = defaultdict(list)
        for s in day_list:
            by_shift[_shift_key(s.time)].append(s)
        for shift_slots in by_shift.values():
            shift_slots.sort(key=lambda s: s.time)
            for s in shift_slots:
                s.constraint_violations = []
                final.append(s)

    dropped = len(slots) - len(final)
    if dropped:
        print(f"    [ENFORCE] {location}: dropped {dropped} slot(s) violating hard limits")
    return final


def _score_slots(slots: List[PlannedSlot], scores_data: dict) -> List[PlannedSlot]:
    score_lookup: Dict[tuple, float] = {}
    fill_lookup: Dict[tuple, float] = {}
    group_score_lookup: Dict[tuple, float] = {}
    group_fill_lookup: Dict[tuple, float] = {}
    day_score_lookup: Dict[tuple, float] = {}
    day_fill_lookup: Dict[tuple, float] = {}
    group_day_score_lookup: Dict[tuple, float] = {}
    group_day_fill_lookup: Dict[tuple, float] = {}
    for r in scores_data.get("slot_group_ranking", []):
        time_str = (r.get("time") or "")[:5]
        day = r.get("day")
        if day is None:
            day = DOW_INT.get(r.get("day_name"), 0)
        key = (r["location"], r["class"], day, time_str)
        group_score_lookup[key] = r.get("score", 30.0)
        group_fill_lookup[key] = r.get("avg_fill_rate", r.get("blended_fill", 0.35))
        day_key = (r["location"], r["class"], day)
        if day_key not in group_day_score_lookup or r.get("score", 30.0) > group_day_score_lookup[day_key]:
            group_day_score_lookup[day_key] = r.get("score", 30.0)
            group_day_fill_lookup[day_key] = r.get("avg_fill_rate", r.get("blended_fill", 0.35))

    for r in scores_data.get("class_slot_ranking", []):
        time_str = (r.get("time") or "")[:5]
        key = (r["location"], r["class"], r["trainer"], r["day"], time_str)
        score_lookup[key] = r.get("score", 30.0)
        fill_lookup[key] = r.get("avg_fill_rate", 0.35)
        day_key = (r["location"], r["class"], r["trainer"], r["day"])
        if day_key not in day_score_lookup or r.get("score", 30.0) > day_score_lookup[day_key]:
            day_score_lookup[day_key] = r.get("score", 30.0)
            day_fill_lookup[day_key] = r.get("avg_fill_rate", 0.35)

    for slot in slots:
        dow = DOW_INT.get(slot.day_of_week, 0)
        exact_key = (slot.location, slot.class_name, slot.trainer_1, dow, slot.time[:5])
        day_key = (slot.location, slot.class_name, slot.trainer_1, dow)
        group_key = (slot.location, slot.class_name, dow, slot.time[:5])
        group_day_key = (slot.location, slot.class_name, dow)
        slot.score = score_lookup.get(
            exact_key,
            group_score_lookup.get(group_key, day_score_lookup.get(day_key, group_day_score_lookup.get(group_day_key, 30.0))),
        )
        slot.predicted_fill_rate = fill_lookup.get(
            exact_key,
            group_fill_lookup.get(group_key, day_fill_lookup.get(day_key, group_day_fill_lookup.get(group_day_key, 0.20))),
        )

    return slots


def _print_utilisation(slots: List[PlannedSlot], profiles_by_name: dict):
    from collections import defaultdict
    DURATIONS = {
        "Studio Barre 57": get_class_duration("Studio Barre 57"),
        "Studio Cardio Barre": get_class_duration("Studio Cardio Barre"),
        "Studio Mat 57": get_class_duration("Studio Mat 57"),
        "Studio PowerCycle": get_class_duration("Studio PowerCycle"),
        "Studio PowerCycle Express": get_class_duration("Studio PowerCycle Express"),
        "Studio FIT": get_class_duration("Studio FIT"),
        "Studio Foundations": get_class_duration("Studio Foundations"),
        "Studio Recovery": get_class_duration("Studio Recovery"),
        "Studio Strength Lab": get_class_duration("Studio Strength Lab"),
        "Studio Back Body Blaze": get_class_duration("Studio Back Body Blaze"),
        "Studio Amped Up!": get_class_duration("Studio Amped Up!"),
        "Studio HIIT": get_class_duration("Studio HIIT"),
        "Studio SWEAT In 30": get_class_duration("Studio SWEAT In 30"),
    }
    minutes: Dict[str, int] = defaultdict(int)
    for s in slots:
        dur = DURATIONS.get(s.class_name, get_class_duration(s.class_name))
        minutes[s.trainer_1] += dur

    t1 = [(n, m) for n, m in minutes.items()
          if profiles_by_name.get(n, {}).get("tier", 3) == 1]
    t1.sort(key=lambda x: -x[1])
    print("  Tier 1 weekly utilisation:")
    for name, mins in t1:
        pct = min(100, int(mins / 900 * 100))
        print(f"    {name:<26} {mins/60:4.1f}h  ({pct}% of 15h target)")


def _daily_target_errors(schedule: List[dict]) -> List[str]:
    config_path = CONFIG_DIR / "schedule_config.json"
    if not config_path.exists():
        return []

    try:
        with open(config_path) as f:
            schedule_config = json.load(f)
    except Exception:
        return []

    errors: List[str] = []
    for location, day_targets in schedule_config.get("targets", {}).items():
        loc_slots = [s for s in schedule if s.get("location") == location]
        for day, limits in day_targets.items():
            if not isinstance(limits, dict):
                continue
            actual = sum(1 for s in loc_slots if s.get("day_of_week") == day)
            target = limits.get("target")
            max_count = limits.get("max")
            if target is not None and actual != int(target):
                errors.append(f"{location} {day}: actual {actual} != target {int(target)}")
            if max_count is not None and actual > int(max_count):
                errors.append(f"{location} {day}: actual {actual} exceeds max {int(max_count)}")
    return errors


def _target_count_for_location(location: str) -> int:
    targets = DAILY_TARGETS.get(location, {day: 7 for day in DAY_NAMES}).copy()
    config_path = CONFIG_DIR / "schedule_config.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                schedule_config = json.load(f)
            for day, limits in (schedule_config.get("targets", {}).get(location, {}) or {}).items():
                if day in targets and isinstance(limits, dict) and limits.get("target") is not None:
                    targets[day] = int(limits["target"])
        except Exception:
            pass
    return sum(targets.values())


def _has_enough_slots_after_enforcement(location: str, slots: List[PlannedSlot]) -> bool:
    target = _target_count_for_location(location)
    if target <= 0:
        return len(slots) >= 20
    return len(slots) >= max(20, int(target * 0.90))


def _enforce_global_trainer_overlaps(slots: List[PlannedSlot], profiles: dict) -> List[PlannedSlot]:
    """Final cross-location guard after all location plans are combined."""
    from collections import defaultdict

    kept: List[PlannedSlot] = []
    trainer_day_slots: Dict[Tuple[str, str], List[PlannedSlot]] = defaultdict(list)
    trainer_minutes: Dict[str, int] = defaultdict(int)
    trainer_worked_days: Dict[str, Set[str]] = defaultdict(set)

    for slot in sorted(slots, key=lambda s: (DAY_NAMES.index(s.day_of_week), s.time, s.location, s.class_name)):
        trainer_key = normalize_trainer_name(slot.trainer_1)
        duration = int(slot.duration_min or get_class_duration(slot.class_name))
        profile = profiles.get(slot.trainer_1) or profiles.get(trainer_key) or {}
        tier = profile.get("tier", 3)
        max_mins = MAX_TRAINER_WEEKLY_MINUTES_T1 if tier == 1 else (MAX_TRAINER_WEEKLY_MINUTES_T2 if tier == 2 else MAX_TRAINER_WEEKLY_MINUTES_T3)
        
        if trainer_minutes[trainer_key] + duration > max_mins:
            continue
            
        # Global 5-day work limit (ensure 2 days off)
        if (
            slot.day_of_week not in trainer_worked_days[trainer_key]
            and len(trainer_worked_days[trainer_key]) >= 5
        ):
            continue

        day_key = (trainer_key, slot.day_of_week)
        start_min = slot_time_to_minutes(slot.time)

        # Ensure single location per day rule
        existing_at_other_loc = any(
            existing.location != slot.location
            for existing in trainer_day_slots[day_key]
        )
        if existing_at_other_loc:
            continue

        if any(
            time_windows_overlap(
                start_min,
                duration,
                slot_time_to_minutes(existing.time),
                int(existing.duration_min or get_class_duration(existing.class_name)),
            )
            for existing in trainer_day_slots[day_key]
        ):
            continue
            
        kept.append(slot)
        trainer_day_slots[day_key].append(slot)
        trainer_minutes[trainer_key] += duration
        trainer_worked_days[trainer_key].add(slot.day_of_week)

    dropped = len(slots) - len(kept)
    if dropped:
        print(f"    [ENFORCE] global trainer guard dropped {dropped} overlapping/cap slot(s)")
    return kept


def _select_primary_iteration(iterations: List[dict]) -> dict:
    if not iterations:
        return {"schedule": []}

    for iteration in iterations:
        if not _daily_target_errors(iteration.get("schedule", [])):
            return iteration

    return min(
        iterations,
        key=lambda iteration: len(_daily_target_errors(iteration.get("schedule", []))),
    )


# ---------------------------------------------------------------------------
# Main planner class
# ---------------------------------------------------------------------------

class AISchedulePlanner:
    """
    Agent 5: AI actively builds the weekly schedule for each location.
    Reads all historical data, scores, constraints, and trainer profiles.
    Falls back to greedy ScheduleOptimiser only when no API key is present.
    """

    def __init__(self, target_week_start: str, locations: List[str] = None,
                 overrides_path: str = None, variation_seed: int = 0,
                 output_suffix: str = ""):
        self.target_week_start = target_week_start
        self.locations = locations or [
            "Kwality House, Kemps Corner",
            "Supreme HQ, Bandra",
            "Kenkere House",
            "Courtside",
            "Copper & Cloves",
        ]
        self.overrides_path = overrides_path
        self.variation_seed = variation_seed
        self.output_suffix = output_suffix

    def _write_draft_output(self, output: dict) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        paths = [STATE_DIR / f"05_draft_schedule{('_' + self.output_suffix) if self.output_suffix else ''}.json"]
        canonical_path = STATE_DIR / "05_draft_schedule.json"
        if canonical_path not in paths:
            paths.append(canonical_path)

        for path in paths:
            atomic_write_json(path, output, indent=2)
        prune_draft_schedule_files(STATE_DIR, keep_groups=5)

    def run(self) -> dict:
        force_ai_only = os.environ.get("SCHEDULER_FORCE_AI_ONLY") == "1"

        if os.environ.get("SCHEDULER_FORCE_GREEDY") == "1":
            print("[Agent 5] Standard generation requested — using greedy optimiser")
            return self._fallback()

        if not OPENAI_AVAILABLE:
            message = "[Agent 5] openai package not installed"
            if force_ai_only:
                raise RuntimeError(f"{message}. Install with: pip install openai")
            print(f"{message} — falling back to greedy optimiser")
            print("[Agent 5]   Install with: pip install openai")
            return self._fallback()

        client, settings = create_ai_client()
        if not client or not settings:
            message = "[Agent 5] API client not configured (key/provider/model)"
            if force_ai_only:
                raise RuntimeError(
                    f"{message}. Set AI API key and runtime settings in Control Center."
                )
            print(f"{message} — falling back to greedy optimiser")
            print("[Agent 5]   Set OPENROUTER_API_KEY/OPENAI_API_KEY and model settings")
            return self._fallback()

        model_name = settings["model"]
        print(f"[Agent 5] AI Planner starting — {model_name} will build each location's schedule...")

        # Load all state files
        try:
            with open(STATE_DIR / "03_scores.json") as f:
                scores_data = json.load(f)
            with open(STATE_DIR / "02_metrics.json") as f:
                metrics_data = json.load(f)
            with open(RULES_DIR / "trainer_profiles.json") as f:
                profiles = json.load(f)
        except FileNotFoundError as e:
            message = f"[Agent 5] Missing file: {e} — run agents 1-4 first"
            if force_ai_only:
                raise RuntimeError(message)
            print(message)
            return self._fallback()

        profiles_by_name = {p["name"]: p for p in profiles}
        rules_config = load_rules_config()
        rules_catalog = build_rules_catalog(rules_config)
        active_rules = [
            category_id
            for category_id, value in rules_catalog["categories"].items()
            if value.get("enabled")
        ]
        print(f"  [Agent 5] Rules active: {', '.join(active_rules)}")
        system_prompt = _build_system_prompt(profiles, rules_catalog)
        all_slots: List[PlannedSlot] = []
        all_errors: List[str] = []
        repaired_locations: List[str] = []

        # Build all prompts up front
        user_prompts = {
            loc: _build_location_prompt(loc, self.target_week_start, scores_data, metrics_data, profiles)
            for loc in self.locations
        }

        def _plan_location(location: str):
            raw = self._call_model(client, model_name, system_prompt, user_prompts[location], location)
            if raw is None:
                return location, None, [f"{location}: AI call failed"]

            slots, errors = _parse_schedule_response(
                raw, location, self.target_week_start, profiles_by_name
            )
            if len(slots) < 20:
                return location, None, [f"{location}: only {len(slots)} slots parsed"]

            slots = _validate_slots(slots, location, profiles_by_name)
            slots = _enforce_hard_limits(slots, location, profiles_by_name)
            if not _has_enough_slots_after_enforcement(location, slots):
                return location, None, [
                    f"{location}: only {len(slots)} slots remained after hard-limit enforcement"
                ]
            slots = _score_slots(slots, scores_data)
            return location, slots, errors

        print(f"  [Agent 5] Calling {model_name} for all {len(self.locations)} locations in parallel...")
        results: Dict[str, tuple] = {}
        with ThreadPoolExecutor(max_workers=len(self.locations)) as pool:
            futures = {pool.submit(_plan_location, loc): loc for loc in self.locations}
            for future in as_completed(futures):
                location, slots, errors = future.result()
                results[location] = (slots, errors)

        for location in self.locations:
            slots, errors = results[location]
            if errors:
                for e in errors[:3]:
                    print(f"    [PARSE] {e}")
                all_errors.extend(errors)

            if slots is None:
                print(f"    [REPAIR] {location} — AI plan invalid, repairing with optimiser")
                slots = self._fallback_location(location, scores_data, profiles_by_name)
                if not slots and force_ai_only:
                    raise RuntimeError(
                        f"[Agent 5] AI-only mode failed at {location}: could not produce a valid AI plan"
                    )
                repaired_locations.append(location)

            violations = sum(1 for s in slots if s.constraint_violations)
            pred_fill = sum(s.predicted_fill_rate for s in slots) / len(slots) if slots else 0
            print(f"    {location}: {len(slots)} slots | {violations} violations | fill≈{pred_fill:.0%}")
            all_slots.extend(slots)

        all_slots = _enforce_global_trainer_overlaps(all_slots, profiles_by_name)
        _print_utilisation(all_slots, profiles_by_name)

        output = {
            "target_week_start": self.target_week_start,
            "schedule": [asdict(s) for s in all_slots],
            "ai_planned": True,
            "ai_repaired_locations": repaired_locations,
            "parse_errors": all_errors,
            "variation_seed": self.variation_seed,
            "output_suffix": self.output_suffix,
        }

        self._write_draft_output(output)

        print(f"[Agent 5] AI Planner complete — {len(all_slots)} total slots across {len(self.locations)} locations")
        return output

    def _call_model(self, client, model_name: str, system_prompt: str, user_prompt: str,
                    location: str) -> Optional[str]:
        """Single OpenRouter/OpenAI-compatible completion call."""
        try:
            response = create_chat_completion(
                client=client,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model_name,
                max_tokens=MAX_TOKENS,
            )
            usage = response.usage
            prompt_tokens = getattr(usage, "prompt_tokens", 0)
            completion_tokens = getattr(usage, "completion_tokens", 0)
            print(f"    [{location.split(',')[0]}] {prompt_tokens}in/{completion_tokens}out")
            message = response.choices[0].message if response.choices else None
            return message.content if message and message.content else None
        except Exception as e:
            print(f"    [ERROR] {location}: {str(e)[:200]}")
            return None

    def _fallback(self) -> dict:
        from agents.optimiser import ScheduleOptimiser
        iteration_configs = [
            ("Max Score", "max_score", 0),
            ("Trainer Hours", "trainer_hours", 42),
            ("Class Variety", "class_variety", 137),
        ]
        base_seed = self.variation_seed or 0
        iterations = []

        for display_name, mode, seed_offset in iteration_configs:
            suffix_bits = [self.output_suffix] if self.output_suffix else []
            suffix_bits.append(mode)
            opt = ScheduleOptimiser(
                target_week_start=self.target_week_start,
                locations=self.locations,
                overrides_path=self.overrides_path,
                variation_seed=base_seed + seed_offset,
                output_suffix="_".join(suffix_bits),
                optimization_mode=mode,
            )
            result = opt.run()
            result["iteration_name"] = display_name
            result["optimization_mode"] = mode
            iterations.append(result)

        primary_iteration = _select_primary_iteration(iterations)

        output = {
            "target_week_start": self.target_week_start,
            "schedule": primary_iteration["schedule"],
            "iterations": iterations,
            "iteration_names": [name for name, _, _ in iteration_configs],
            "selected_iteration_name": primary_iteration.get("iteration_name"),
            "variation_seed": self.variation_seed,
            "output_suffix": self.output_suffix,
        }
        self._write_draft_output(output)
        return output

    def _fallback_location(self, location: str, scores_data: dict,
                           profiles_by_name: dict) -> List[PlannedSlot]:
        try:
            from agents.optimiser import ScheduleOptimiser
            opt = ScheduleOptimiser(
                target_week_start=self.target_week_start,
                locations=[location],
                overrides_path=self.overrides_path,
                variation_seed=self.variation_seed,
                output_suffix=f"{self.output_suffix}_fallback" if self.output_suffix else "fallback",
                optimization_mode="max_score",
            )
            result = opt.run()
            slots = []
            for s in result.get("schedule", []):
                slots.append(PlannedSlot(
                    location=s["location"], date=s["date"],
                    day_of_week=s["day_of_week"], time=s["time"],
                    class_name=s["class_name"], trainer_1=s["trainer_1"],
                    trainer_2=s.get("trainer_2", ""), cover=s.get("cover", ""),
                    room=s.get("room", ""), capacity=s.get("capacity", 15),
                    predicted_fill_rate=s.get("predicted_fill_rate", 0.35),
                    score=s.get("score", 30.0),
                    constraint_violations=s.get("constraint_violations", []),
                    duration_min=int(s.get("duration_min") or get_class_duration(s.get("class_name", ""))),
                    rationale="greedy_fallback",
                ))
            return slots
        except Exception as e:
            print(f"    [ERROR] Greedy fallback also failed for {location}: {e}")
            return []
