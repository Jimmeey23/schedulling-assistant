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
from typing import Dict, List, Optional, Tuple

from ai_provider import OPENAI_AVAILABLE, create_ai_client, create_chat_completion, get_ai_settings
from rule_config import build_rules_catalog, get_active_format_rules, load_rules_config

STATE_DIR = Path("state")
RULES_DIR = Path("rules")

MAX_TOKENS = 12000  # Supreme needs ~9k for 68 verbose slots; others ~3.5k

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DOW_INT = {d: i for i, d in enumerate(DAY_NAMES)}

# Historical data-driven daily class count targets
DAILY_TARGETS = {
    "Kwality House, Kemps Corner": {
        "Monday": 12, "Tuesday": 10, "Wednesday": 11,
        "Thursday": 11, "Friday": 9, "Saturday": 10, "Sunday": 6,
    },
    "Supreme HQ, Bandra": {
        "Monday": 11, "Tuesday": 10, "Wednesday": 9,
        "Thursday": 11, "Friday": 10, "Saturday": 11, "Sunday": 6,
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
               "18:30", "19:00", "19:15", "19:30", "20:00"],
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
    rationale: str = ""


# ---------------------------------------------------------------------------
# System prompt — comprehensive rules, trainer profiles, class formats
# ---------------------------------------------------------------------------

def _build_system_prompt(profiles: list, rules_catalog: dict) -> str:
    category_map = rules_catalog["categories"]
    group_map = {group["id"]: group for group in rules_catalog["groups"]}
    trainer_specific_on = category_map["trainer_specific"]["enabled"]

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

    location_sections = [
        section for section in [
            render_rule_block("location_kwality", "KWALITY HOUSE:"),
            render_rule_block("location_supreme", "SUPREME HQ:"),
            render_rule_block("location_kenkere", "KENKERE HOUSE:"),
            render_rule_block("trainer_specific", "TRAINER-SPECIFIC RULES:"),
        ] if section
    ]

    if trainer_specific_on:
        trainer_day_note = "TRAINER DAYS ARE HARD RULES — schedule trainers ONLY on their listed days."
    else:
        trainer_day_note = ("TRAINER DAYS are typical availability for scheduling preference only "
                            "— not hard restrictions. Use best-fit based on performance data.")

    universal_block = render_rule_block("universal", "HARD CONSTRAINTS — ALL LOCATIONS:")

    formats_block = ""
    if category_map["format_rules"]["enabled"] and fmt_lines:
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
    sections.extend(location_sections)
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

    # Top performers for this location (fill ≥ 28%, trainer ran ≥ 5 sessions)
    # Sort by blended score first, then by recency-boosted score
    loc_top = [
        r for r in ranking
        if r["location"] == location
        and r.get("avg_fill_rate", 0) >= 0.28
        and (r.get("trainer_total_sessions") or r.get("session_count", 0)) >= 5
    ]
    loc_top.sort(key=lambda x: (-x["score"], -x.get("avg_fill_rate", 0)))

    # Bottom combos to avoid (fill < 22%, trainer ran ≥ 5 sessions)
    loc_avoid = [
        r for r in ranking
        if r["location"] == location
        and r.get("avg_fill_rate", 0) < 0.22
        and (r.get("trainer_total_sessions") or r.get("session_count", 0)) >= 5
    ]
    loc_avoid.sort(key=lambda x: x.get("avg_fill_rate", 0))

    # Trainer performance at this location
    loc_trainers = [t for t in trainer_metrics if t["location"] == location]
    loc_trainers.sort(key=lambda x: -x.get("trainer_avg_checkin", 0))

    targets = DAILY_TARGETS.get(location, {d: 7 for d in DAY_NAMES})
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
        trainer_specific_on = rules_config["categories"]["trainer_specific"]["enabled"]
        avail_header = (
            "### TRAINER AVAILABILITY — HARD LOCKS (only schedule on listed days):"
            if trainer_specific_on else
            "### TRAINER AVAILABILITY — default preference (respect unless strong historical reason):"
        )
        lines += ["", avail_header]
        for p in profiles:
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
        'Schema: {"location":"...","week_start":"...","schedule":[{"day":"Monday","time":"08:30","class":"Studio Barre 57","trainer":"Anisha Shah","cover":"Mrigakshi Jaiswal"},...]}',
        "",
        "CRITICAL: ALL 7 days. Hit exact daily targets. Use exact class/trainer names from above. Place owned blocks first. No duplicate times per day. Every slot needs a cover trainer. Barre family ≥45%.",
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
    seen_times: Dict[str, set] = {d: set() for d in DAY_NAMES}

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

        # Normalize HH:MM
        time_str = time_str.strip()
        if len(time_str) == 4 and time_str[1] == ":":
            time_str = "0" + time_str

        if time_str in seen_times[day]:
            errors.append(f"Duplicate time {time_str} on {day} — keeping first")
            continue
        seen_times[day].add(time_str)

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
            rationale=rationale,
        ))

    return slots, errors


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
        loc_data = prof.get("locations", {}).get(location, {})
        avail_days = loc_data.get("available_days", [])
        if avail_days and slot.day_of_week not in avail_days:
            v.append(f"UNIV-020: {slot.trainer_1} not available at {location} on {slot.day_of_week}")

        if location == "Kenkere House" and "PowerCycle" in slot.class_name:
            v.append("UNIV-011/KE-001: PowerCycle never at Kenkere House")

        if "Strength Lab" in slot.class_name:
            if location != "Kwality House, Kemps Corner":
                v.append("UNIV-012: Strength Lab only at Kwality")
            if slot.trainer_1 != "Atulan Purohit":
                v.append("KW-006: Strength Lab requires Atulan Purohit exclusively")

        if slot.day_of_week == "Sunday":
            h, m = map(int, slot.time.split(":"))
            if h < 10:
                v.append("UNIV-004: No Sunday class before 10:00")

        max_day = loc_data.get("max_classes_per_day", 4)
        if day_trainer_count[slot.day_of_week][slot.trainer_1] > max_day:
            v.append(f"UNIV-010: {slot.trainer_1} exceeds daily limit ({max_day}) on {slot.day_of_week}")

    return slots


def _enforce_hard_limits(slots: List[PlannedSlot], location: str,
                         profiles_by_name: dict = None) -> List[PlannedSlot]:
    """Drop slots violating hard weekly caps, day/time rules, and trainer availability."""
    from collections import defaultdict

    # Build trainer availability lookup
    trainer_allowed_days: Dict[str, set] = {}
    if profiles_by_name:
        for name, prof in profiles_by_name.items():
            loc_data = prof.get("locations", {}).get(location, {})
            avail = loc_data.get("available_days")
            if avail:
                trainer_allowed_days[name] = set(avail)

    kept = []
    sl_count = 0

    for slot in slots:
        # Strength Lab: max 2/week at Kwality, 0 elsewhere
        if "Strength Lab" in slot.class_name:
            if location != "Kwality House, Kemps Corner":
                continue
            sl_count += 1
            if sl_count > 2:
                continue
        # PowerCycle: never at Kenkere
        if location == "Kenkere House" and "PowerCycle" in slot.class_name:
            continue
        # Sunday: no evening band (17:00+), no class before 10:00
        if slot.day_of_week == "Sunday":
            h = int(slot.time[:2])
            if h >= 17 or h < 10:
                continue
        # Trainer must be available on that day
        allowed = trainer_allowed_days.get(slot.trainer_1)
        if allowed and slot.day_of_week not in allowed:
            continue
        kept.append(slot)

    # Recovery must be last in its shift (morning=7-12, midday=12-17, evening=17+)
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
            last_time = shift_slots[-1].time
            for s in shift_slots:
                if "Recovery" in s.class_name and s.time != last_time:
                    continue  # drop Recovery that isn't last
                final.append(s)

    dropped = len(slots) - len(final)
    if dropped:
        print(f"    [ENFORCE] {location}: dropped {dropped} slot(s) violating hard limits")
    return final


def _score_slots(slots: List[PlannedSlot], scores_data: dict) -> List[PlannedSlot]:
    score_lookup: Dict[tuple, float] = {}
    fill_lookup: Dict[tuple, float] = {}
    for r in scores_data.get("class_slot_ranking", []):
        key = (r["location"], r["class"], r["trainer"], r["day"])
        score_lookup[key] = r.get("score", 30.0)
        fill_lookup[key] = r.get("avg_fill_rate", 0.35)

    for slot in slots:
        dow = DOW_INT.get(slot.day_of_week, 0)
        key = (slot.location, slot.class_name, slot.trainer_1, dow)
        slot.score = score_lookup.get(key, 30.0)
        slot.predicted_fill_rate = fill_lookup.get(key, 0.35)

    return slots


def _print_utilisation(slots: List[PlannedSlot], profiles_by_name: dict):
    from collections import defaultdict
    DURATIONS = {
        "Studio Barre 57": 57, "Studio Cardio Barre": 57, "Studio Mat 57": 57,
        "Studio PowerCycle": 45, "Studio PowerCycle Express": 30,
        "Studio FIT": 45, "Studio Foundations": 45, "Studio Recovery": 45,
        "Studio Strength Lab": 57, "Studio Back Body Blaze": 57,
        "Studio Amped Up!": 57, "Studio HIIT": 45, "Studio SWEAT In 30": 30,
    }
    minutes: Dict[str, int] = defaultdict(int)
    for s in slots:
        dur = DURATIONS.get(s.class_name, 57)
        minutes[s.trainer_1] += dur

    t1 = [(n, m) for n, m in minutes.items()
          if profiles_by_name.get(n, {}).get("tier", 3) == 1]
    t1.sort(key=lambda x: -x[1])
    print("  Tier 1 weekly utilisation:")
    for name, mins in t1:
        pct = min(100, int(mins / 900 * 100))
        print(f"    {name:<26} {mins/60:4.1f}h  ({pct}% of 15h target)")


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
        ]
        self.overrides_path = overrides_path
        # variation_seed and output_suffix kept for compatibility but not used
        self.output_suffix = output_suffix

    def run(self) -> dict:
        if not OPENAI_AVAILABLE:
            print("[Agent 5] openai package not installed — falling back to greedy optimiser")
            print("[Agent 5]   Install with: pip install openai")
            return self._fallback()

        client, settings = create_ai_client()
        if not client or not settings:
            print("[Agent 5] OPENROUTER_API_KEY not set — falling back to greedy optimiser")
            print("[Agent 5]   Set OPENROUTER_API_KEY and optionally OPENROUTER_MODEL in .env or your shell")
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
            print(f"[Agent 5] Missing file: {e} — run agents 1-4 first")
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
                print(f"    [WARN] {location} — falling back to greedy")
                slots = self._fallback_location(location, scores_data, profiles_by_name)

            violations = sum(1 for s in slots if s.constraint_violations)
            pred_fill = sum(s.predicted_fill_rate for s in slots) / len(slots) if slots else 0
            print(f"    {location}: {len(slots)} slots | {violations} violations | fill≈{pred_fill:.0%}")
            all_slots.extend(slots)

        _print_utilisation(all_slots, profiles_by_name)

        output = {
            "target_week_start": self.target_week_start,
            "schedule": [asdict(s) for s in all_slots],
            "ai_planned": True,
            "parse_errors": all_errors,
        }

        STATE_DIR.mkdir(exist_ok=True)
        out_path = STATE_DIR / "05_draft_schedule.json"
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2)

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
        opt = ScheduleOptimiser(
            target_week_start=self.target_week_start,
            locations=self.locations,
            overrides_path=self.overrides_path,
            variation_seed=0,
            output_suffix="",
        )
        return opt.run()

    def _fallback_location(self, location: str, scores_data: dict,
                           profiles_by_name: dict) -> List[PlannedSlot]:
        try:
            from agents.optimiser import ScheduleOptimiser
            opt = ScheduleOptimiser(
                target_week_start=self.target_week_start,
                locations=[location],
                overrides_path=self.overrides_path,
                variation_seed=0,
                output_suffix="_fallback",
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
                    rationale="greedy_fallback",
                ))
            return slots
        except Exception as e:
            print(f"    [ERROR] Greedy fallback also failed for {location}: {e}")
            return []
