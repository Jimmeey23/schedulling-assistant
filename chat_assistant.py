import json
import re
from pathlib import Path

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

LOCATION_ALIASES = {
    "kwality": "Kwality House, Kemps Corner",
    "kemps": "Kwality House, Kemps Corner",
    "supreme": "Supreme HQ, Bandra",
    "bandra": "Supreme HQ, Bandra",
    "kenkere": "Kenkere House",
    "copper": "Copper & Cloves",
    "cloves": "Copper & Cloves",
    "courtside": "Courtside",
}


def _load_json(path: Path, fallback):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return fallback


def _norm(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _same_name(a: str, b: str) -> bool:
    return _norm(a).lower() == _norm(b).lower()


def _slot_minutes(value: str) -> int:
    hour, minute = str(value or "00:00").split(":")[:2]
    return int(hour) * 60 + int(minute)


def _canonical_query_terms(message: str) -> set[str]:
    lower = str(message or "").lower()
    terms = set(re.findall(r"[a-z0-9']+", lower))
    synonyms = {
        "strength": {"strength", "lab"},
        "cycle": {"powercycle", "cycle"},
        "spin": {"powercycle", "cycle"},
        "mat": {"mat"},
        "barre": {"barre"},
        "fit": {"fit"},
        "cardio": {"cardio", "barre"},
    }
    for key, extra in synonyms.items():
        if key in terms:
            terms.update(extra)
    return {term for term in terms if len(term) > 2}


def _parse_query(message: str) -> dict:
    lower = str(message or "").lower()
    day = next((name for name in DAY_NAMES if name.lower() in lower), "")
    location = next((loc for alias, loc in LOCATION_ALIASES.items() if alias in lower), "")
    time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", lower)
    if not time_match:
        time_match = re.search(r"\b(\d{1,2}):(\d{2})\b", lower)
    time = ""
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        suffix = time_match.group(3) if len(time_match.groups()) >= 3 else None
        if suffix == "pm" and hour != 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
        time = f"{hour:02d}:{minute:02d}"
    return {"day": day, "location": location, "time": time, "terms": _canonical_query_terms(message)}


def _detect_intent(message: str) -> str:
    lower = str(message or "").lower()
    if any(term in lower for term in ("substitute", "substitution", "cover", "replace", "swap", "fill in")):
        return "substitution"
    if any(term in lower for term in ("lowest fill", "low fill", "underperform", "improve", "fix", "weak classes")):
        return "low_fill"
    if any(term in lower for term in ("workload", "weekly limit", "hours", "overloaded", "overuse", "day off")):
        return "workload"
    if any(term in lower for term in ("add class", "add a class", "new class", "recommend class", "class idea")):
        return "add_class"
    if any(term in lower for term in ("why", "explain", "constraint", "issue", "badge")):
        return "explain"
    return "general"


def _all_schedule_rows(schedule_data: dict) -> list[dict]:
    rows = []
    for loc, loc_rows in (schedule_data.get("locations") or {}).items():
        for row in loc_rows or []:
            if not row.get("location"):
                row = {**row, "location": loc}
            rows.append(row)
    return rows


def _row_text(row: dict) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("location", "day_of_week", "time", "class_name", "trainer_1", "room", "recommendation")
    ).lower()


def _row_summary(row: dict) -> str:
    fill = row.get("metric_avg_fill_rate", row.get("historical_avg_fill", row.get("predicted_fill_rate", 0))) or 0
    checkin = row.get("metric_avg_checkin", row.get("historical_avg_checkin", 0)) or 0
    sessions = row.get("metric_session_count", row.get("historical_session_count", 0)) or 0
    return (
        f"{row.get('day_of_week')} {row.get('time')} | {row.get('location')} | "
        f"{row.get('class_name')} | trainer={row.get('trainer_1') or '-'} | "
        f"duration={row.get('duration_min') or 57}m | room={row.get('room') or '-'} | "
        f"score={round(float(row.get('score') or 0), 1)} | fill={float(fill):.0%} | "
        f"checkin={float(checkin):.1f} | sessions={sessions}"
    )


def _profile_for(profiles: list[dict], trainer: str) -> dict:
    return next((p for p in profiles if _same_name(p.get("name"), trainer)), {})


def _trainer_day_assignments(rows: list[dict], trainer: str, day: str) -> list[dict]:
    return sorted(
        [
            row for row in rows
            if _same_name(row.get("trainer_1"), trainer)
            and (not day or row.get("day_of_week") == day)
        ],
        key=lambda row: (row.get("day_of_week") or "", row.get("time") or ""),
    )


def _weekly_minutes(rows: list[dict], trainer: str) -> int:
    return sum(int(row.get("duration_min") or 57) for row in rows if _same_name(row.get("trainer_1"), trainer))


def _trainer_weekly_sessions(rows: list[dict], trainer: str) -> int:
    return sum(1 for row in rows if _same_name(row.get("trainer_1"), trainer))


def _slot_duration(row: dict) -> int:
    return int(row.get("duration_min") or 57)


def _slot_end_minutes(row: dict) -> int:
    return _slot_minutes(row.get("time") or "00:00") + _slot_duration(row)


def _rows_overlap(a: dict, b: dict) -> bool:
    if (a.get("day_of_week") or "") != (b.get("day_of_week") or ""):
        return False
    a_start = _slot_minutes(a.get("time") or "00:00")
    b_start = _slot_minutes(b.get("time") or "00:00")
    return a_start < _slot_end_minutes(b) and b_start < _slot_end_minutes(a)


def _shift_label(time_value: str) -> str:
    minutes = _slot_minutes(time_value or "00:00")
    if minutes < 12 * 60:
        return "morning"
    if minutes < 16 * 60:
        return "afternoon"
    return "evening"


def _row_compact(row: dict) -> str:
    return f"{row.get('time')} {row.get('location')} {row.get('class_name')}"


def _row_metric_compact(row: dict) -> str:
    fill = row.get("metric_avg_fill_rate", row.get("historical_avg_fill", row.get("predicted_fill_rate", 0))) or 0
    sessions = row.get("metric_session_count", row.get("historical_session_count", 0)) or 0
    return (
        f"{row.get('day_of_week')} {row.get('time')} {row.get('location')} {row.get('class_name')} "
        f"score={round(float(row.get('score') or 0), 1)} fill={float(fill):.0%} sessions={sessions}"
    )


def _trainer_relevant_history(rows: list[dict], trainer: str, terms: set[str], location: str) -> str:
    matches = []
    for row in rows:
        if not _same_name(row.get("trainer_1"), trainer):
            continue
        text = _row_text(row)
        if terms and not any(term in text for term in terms):
            continue
        score = float(row.get("score") or 0)
        if location and row.get("location") == location:
            score += 5
        matches.append((score, row))
    matches.sort(key=lambda item: -item[0])
    if not matches:
        return "no matching schedule metrics found"
    return "; ".join(_row_metric_compact(row) for _, row in matches[:3])


def _row_fill(row: dict) -> float:
    return float(row.get("metric_avg_fill_rate", row.get("historical_avg_fill", row.get("predicted_fill_rate", 0))) or 0)


def _row_sessions(row: dict) -> int:
    return int(row.get("metric_session_count", row.get("historical_session_count", row.get("slot_session_count", 0))) or 0)


def _query_qualification_keys(terms: set[str]) -> set[str]:
    keys = set()
    if {"strength", "lab"} & terms:
        keys.add("strength_lab")
    if {"powercycle", "cycle"} & terms:
        keys.add("powercycle")
    if "mat" in terms:
        keys.add("mat_57")
    if {"barre", "cardio", "fit"} & terms:
        keys.add("all_barre")
    if "hiit" in terms:
        keys.add("hiit")
    if "amped" in terms:
        keys.add("amped_up")
    if "recovery" in terms:
        keys.add("recovery")
    if "foundation" in terms:
        keys.add("foundations")
    return keys


def _history_terms_for_qual_keys(keys: set[str]) -> set[str]:
    mapping = {
        "strength_lab": {"strength", "lab"},
        "powercycle": {"powercycle", "cycle"},
        "mat_57": {"mat"},
        "all_barre": {"barre", "cardio", "fit"},
        "hiit": {"hiit"},
        "amped_up": {"amped"},
        "recovery": {"recovery"},
        "foundations": {"foundation"},
    }
    terms = set()
    for key in keys:
        terms.update(mapping.get(key, set()))
    return terms


def _location_availability_reasons(profile: dict, location: str, day: str, time: str) -> list[str]:
    if not location:
        return []
    loc_data = (profile.get("locations") or {}).get(location)
    if not loc_data:
        return [f"no profile for {location}"]
    reasons = []
    available_days = loc_data.get("available_days") or []
    if day and available_days and day not in available_days:
        reasons.append(f"not available on {day} at {location}")
    window = loc_data.get("time_window") or {}
    start = window.get("start")
    end = window.get("end")
    if time and start and end:
        slot_min = _slot_minutes(time)
        if slot_min < _slot_minutes(start) or slot_min >= _slot_minutes(end):
            reasons.append(f"outside {location} window {start}-{end}")
    return reasons


def _ranked_substitution_candidates(
    rows: list[dict],
    profiles: list[dict],
    qual_keys: set[str],
    terms: set[str],
    requested_slot: dict,
    day: str,
    location: str,
    time: str,
) -> list[str]:
    history_terms = _history_terms_for_qual_keys(qual_keys) or terms
    requested_shift = _shift_label(requested_slot.get("time") or "00:00") if time else ""
    requested_duration = _slot_duration(requested_slot)
    candidates = []

    for profile in profiles:
        if profile.get("active") is False:
            continue
        quals = profile.get("qualifications") or {}
        if qual_keys and not any(quals.get(key) for key in qual_keys):
            continue
        trainer = profile.get("name", "")
        if not trainer:
            continue

        assignments = _trainer_day_assignments(rows, trainer, day) if day else []
        overlaps = [row for row in assignments if time and _rows_overlap(requested_slot, row)]
        same_location_same_shift = [
            row for row in assignments
            if location
            and row.get("location") == location
            and _shift_label(row.get("time") or "00:00") == requested_shift
            and not _rows_overlap(requested_slot, row)
        ]
        different_location_same_day = any(location and row.get("location") != location for row in assignments)
        different_shift_same_day = any(
            requested_shift and _shift_label(row.get("time") or "00:00") != requested_shift
            for row in assignments
        )
        already_teaching_same_class = any(
            row.get("location") == requested_slot.get("location")
            and row.get("day_of_week") == requested_slot.get("day_of_week")
            and str(row.get("time") or "")[:5] == str(requested_slot.get("time") or "")[:5]
            and row.get("class_name") == requested_slot.get("class_name")
            for row in assignments
        )
        projected_sessions = len(assignments) + (0 if already_teaching_same_class else 1)
        projected_hours = (
            _weekly_minutes(rows, trainer)
            + (0 if already_teaching_same_class else requested_duration)
        ) / 60

        blocked_reasons = []
        blocked_reasons.extend(_location_availability_reasons(profile, location, day, time))
        if overlaps:
            blocked_reasons.append("overlaps " + "; ".join(_row_compact(row) for row in overlaps[:3]))
        if different_location_same_day:
            blocked_reasons.append("already scheduled at a different location that day")
        if different_shift_same_day:
            blocked_reasons.append("already scheduled in a different shift that day")
        if projected_sessions > 4:
            blocked_reasons.append(f"would exceed 4 sessions that day ({projected_sessions})")
        if projected_hours > 15:
            blocked_reasons.append(f"would exceed 15 weekly hours ({projected_hours:.1f})")

        status = "blocked" if blocked_reasons else "eligible"
        track_record = _trainer_relevant_history(rows, trainer, history_terms, location)
        track_score = 0.0
        for row in rows:
            if _same_name(row.get("trainer_1"), trainer):
                row_text = _row_text(row)
                if history_terms and any(term in row_text for term in history_terms):
                    track_score = max(track_score, float(row.get("score") or 0))
        score = 0.0
        score += max(0, 5 - int(profile.get("tier", 3))) * 10
        score += 25 if same_location_same_shift else 0
        score += min(track_score, 100) / 4
        score += max(0, 15 - projected_hours)
        if status == "blocked":
            score -= 1000

        assignment_text = "; ".join(_row_compact(row) for row in assignments[:8]) or ("none" if day else "not filtered")
        loc_data = (profile.get("locations") or {}).get(location, {}) if location else {}
        window = loc_data.get("time_window") or {}
        available_days = ", ".join((loc_data.get("available_days") or [])[:7]) if loc_data else "no profile"
        line = (
            f"recommendation_status={status} | trainer={trainer} | tier={profile.get('tier', '-')} "
            f"| rank_score={score:.1f} | matching_quals={', '.join(sorted(qual_keys)) or 'not specified'} "
            f"| blocked_reasons={'; '.join(blocked_reasons) or 'none'} "
            f"| same_location_same_shift_nonoverlap={'; '.join(_row_compact(row) for row in same_location_same_shift) or 'none'} "
            f"| sessions_if_added={projected_sessions} | weekly_hours_if_added={projected_hours:.1f} "
            f"| {day or 'same-day'} assignments={assignment_text} "
            f"| location_profile={available_days} {window.get('start', '')}-{window.get('end', '')} "
            f"| matching_track_record={track_record}"
        )
        candidates.append((status != "eligible", -score, int(profile.get("tier", 9)), trainer, line))

    ranked = []
    for idx, (_, _, _, _, line) in enumerate(sorted(candidates)[:12], start=1):
        ranked.append(f"- rank={idx} | {line}")
    return ranked


def _low_fill_evidence(rows: list[dict], location: str, terms: set[str]) -> list[str]:
    filtered = []
    for row in rows:
        if location and row.get("location") != location:
            continue
        row_text = _row_text(row)
        if terms and not any(term in row_text for term in terms if term not in {"lowest", "low", "fill", "rate", "fix"}):
            pass
        filtered.append(row)
    filtered.sort(key=lambda row: (_row_fill(row), -_row_sessions(row), row.get("day_of_week") or "", row.get("time") or ""))
    lines = ["LOW-FILL / IMPROVEMENT EVIDENCE:"]
    lines.append(
        "lowest_fill_classes="
        + " || ".join(_row_summary(row) for row in filtered[:10])
        if filtered else "lowest_fill_classes=none"
    )
    fixable = [
        row for row in filtered
        if _row_sessions(row) >= 8 and _row_fill(row) < 0.45
    ]
    if fixable:
        lines.append("high_confidence_underperformers=" + " || ".join(_row_summary(row) for row in fixable[:6]))
    return lines


def _workload_evidence(rows: list[dict], profiles: list[dict], parsed: dict) -> list[str]:
    terms = parsed.get("terms") or set()
    trainer_filter = ""
    for profile in profiles:
        name = str(profile.get("name") or "")
        name_terms = set(re.findall(r"[a-z0-9']+", name.lower()))
        if name_terms and len(name_terms & terms) >= min(2, len(name_terms)):
            trainer_filter = name
            break
    trainer_names = [trainer_filter] if trainer_filter else sorted({
        row.get("trainer_1") for row in rows if row.get("trainer_1")
    })
    entries = []
    for trainer in trainer_names:
        assignments = [row for row in rows if _same_name(row.get("trainer_1"), trainer)]
        if not assignments:
            continue
        day_counts = {}
        days = set()
        for row in assignments:
            day = row.get("day_of_week") or "-"
            day_counts[day] = day_counts.get(day, 0) + 1
            days.add(day)
        max_day = max(day_counts.values()) if day_counts else 0
        entries.append((
            -_weekly_minutes(rows, trainer),
            trainer,
            f"- {trainer}: weekly_hours={_weekly_minutes(rows, trainer) / 60:.1f} | "
            f"weekly_sessions={_trainer_weekly_sessions(rows, trainer)} | days_scheduled={len(days)} | "
            f"max_sessions_in_day={max_day} | day_counts={day_counts}"
        ))
    entries.sort()
    return ["TRAINER WORKLOAD EVIDENCE:"] + [line for _, _, line in entries[:12]]


def _decision_evidence(exact_slot: dict | None, relevant: list[dict]) -> list[str]:
    slots = []
    if exact_slot:
        slots.append(exact_slot)
    for row in relevant:
        if row not in slots:
            slots.append(row)
        if len(slots) >= 4:
            break
    lines = ["DECISION EXPLANATION EVIDENCE:"]
    if not slots:
        lines.append("decision_rows=none")
        return lines
    for row in slots:
        violations = row.get("constraint_violations") or []
        breakdown = row.get("score_breakdown") or row.get("metric_score_breakdown") or {}
        components = breakdown.get("bonus_components") or []
        component_text = "; ".join(
            f"{item.get('label') or item.get('key')}={item.get('points')}"
            for item in components[:4]
        ) or "none"
        lines.append(
            f"- {_row_summary(row)} | recommendation={row.get('recommendation') or '-'} "
            f"| scheduling_reason={row.get('scheduling_reason') or '-'} "
            f"| constraint_violations={'; '.join(map(str, violations)) or 'none'} "
            f"| score_components={component_text}"
        )
    return lines


def _build_relevant_evidence(message: str, schedule_data: dict, profiles: list[dict]) -> str:
    rows = _all_schedule_rows(schedule_data)
    if not rows or not message:
        return ""

    parsed = _parse_query(message)
    intent = _detect_intent(message)
    terms = parsed["terms"]
    day = parsed["day"]
    location = parsed["location"]
    time = parsed["time"]

    scored = []
    for row in rows:
        score = 0
        if day and row.get("day_of_week") == day:
            score += 5
        if location and row.get("location") == location:
            score += 5
        if time and str(row.get("time") or "")[:5] == time:
            score += 8
        text = _row_text(row)
        score += sum(1 for term in terms if term in text)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], item[1].get("day_of_week") or "", item[1].get("time") or ""))
    relevant = [row for _, row in scored[:18]]

    exact_slot = None
    if day and location and time:
        exact_matches = [
            row for row in rows
            if row.get("day_of_week") == day
            and row.get("location") == location
            and str(row.get("time") or "")[:5] == time
        ]
        if terms:
            term_matches = [row for row in exact_matches if any(term in _row_text(row) for term in terms)]
            exact_slot = term_matches[0] if term_matches else (exact_matches[0] if exact_matches else None)
        elif exact_matches:
            exact_slot = exact_matches[0]
        if exact_slot and exact_slot not in relevant:
            relevant.insert(0, exact_slot)

    lines = [
        f"DETECTED QUESTION INTENT: {intent}",
        "RELEVANT SCHEDULE EVIDENCE (use this for concrete suggestions; do not invent facts):",
    ]
    if exact_slot:
        lines.append(f"Requested slot: {_row_summary(exact_slot)}")
    if relevant:
        lines.append("Relevant class rows:")
        for row in relevant[:12]:
            lines.append(f"- {_row_summary(row)}")

    if day:
        trainer_names = []
        for row in relevant[:12]:
            trainer = row.get("trainer_1")
            if trainer and trainer not in trainer_names:
                trainer_names.append(trainer)
        if exact_slot and exact_slot.get("trainer_1") not in trainer_names:
            trainer_names.insert(0, exact_slot.get("trainer_1"))
        if trainer_names:
            lines.append("Trainer same-day assignments and load:")
            for trainer in trainer_names[:10]:
                profile = _profile_for(profiles, trainer)
                quals = ", ".join(key for key, enabled in (profile.get("qualifications") or {}).items() if enabled) or "-"
                assignments = _trainer_day_assignments(rows, trainer, day)
                assignment_text = "; ".join(
                    f"{row.get('time')} {row.get('location')} {row.get('class_name')}"
                    for row in assignments[:8]
                ) or "none"
                lines.append(
                    f"- {trainer}: tier={profile.get('tier', '-')} | quals={quals} | "
                    f"{day} assignments={assignment_text} | weekly_hours={_weekly_minutes(rows, trainer) / 60:.1f}"
                )

    if intent == "low_fill":
        lines.extend(_low_fill_evidence(rows, location, terms))

    if intent == "workload":
        lines.extend(_workload_evidence(rows, profiles, parsed))

    if intent == "explain":
        lines.extend(_decision_evidence(exact_slot, relevant))

    qual_keys = _query_qualification_keys(terms)
    if qual_keys:
        history_terms = _history_terms_for_qual_keys(qual_keys) or terms
        candidate_lines = []
        requested_slot = exact_slot or {
            "day_of_week": day,
            "location": location,
            "time": time,
            "duration_min": 57,
            "class_name": "requested class",
        }
        requested_shift = _shift_label(requested_slot.get("time") or "00:00") if time else ""
        requested_duration = _slot_duration(requested_slot)
        if intent == "substitution":
            ranked_candidates = _ranked_substitution_candidates(
                rows,
                profiles,
                qual_keys,
                terms,
                requested_slot,
                day,
                location,
                time,
            )
            if ranked_candidates:
                lines.append("RANKED SUBSTITUTION CANDIDATES (eligible first; blocked candidates explain why):")
                lines.extend(ranked_candidates)
        for profile in profiles:
            if profile.get("active") is False:
                continue
            quals = profile.get("qualifications") or {}
            if not any(quals.get(key) for key in qual_keys):
                continue
            trainer = profile.get("name", "")
            if not trainer:
                continue
            loc_data = (profile.get("locations") or {}).get(location, {}) if location else {}
            loc_note = ""
            if location:
                days = ", ".join((loc_data.get("available_days") or [])[:7]) if loc_data else "no profile for this location"
                window = loc_data.get("time_window") or {}
                loc_note = f" | location_profile={days} {window.get('start', '')}-{window.get('end', '')}"
            assignments = _trainer_day_assignments(rows, trainer, day) if day else []
            assignment_text = "; ".join(
                _row_compact(row)
                for row in assignments[:8]
            ) or ("none" if day else "not filtered")
            overlaps = [row for row in assignments if time and _rows_overlap(requested_slot, row)]
            same_location_same_shift = [
                row for row in assignments
                if location
                and row.get("location") == location
                and _shift_label(row.get("time") or "00:00") == requested_shift
                and not _rows_overlap(requested_slot, row)
            ]
            different_location_same_day = any(location and row.get("location") != location for row in assignments)
            different_shift_same_day = any(
                requested_shift and _shift_label(row.get("time") or "00:00") != requested_shift
                for row in assignments
            )
            already_teaching_requested_slot = any(
                row.get("location") == requested_slot.get("location")
                and row.get("day_of_week") == requested_slot.get("day_of_week")
                and str(row.get("time") or "")[:5] == str(requested_slot.get("time") or "")[:5]
                for row in assignments
            )
            projected_sessions = len(assignments) + (0 if already_teaching_requested_slot else 1)
            projected_hours = (
                _weekly_minutes(rows, trainer)
                + (0 if already_teaching_requested_slot else requested_duration)
            ) / 60
            track_record = _trainer_relevant_history(rows, trainer, history_terms, location)
            candidate_lines.append(
                (
                    profile.get("tier", 9),
                    trainer,
                    f"- {trainer}: tier={profile.get('tier', '-')} | matching_quals={', '.join(sorted(qual_keys))} "
                    f"| overlap_conflicts={'; '.join(_row_compact(row) for row in overlaps) or 'none'} "
                    f"| same_location_same_shift_nonoverlap={'; '.join(_row_compact(row) for row in same_location_same_shift) or 'none'} "
                    f"| different_location_same_day={'yes' if different_location_same_day else 'no'} "
                    f"| different_shift_same_day={'yes' if different_shift_same_day else 'no'} "
                    f"| sessions_if_added={projected_sessions} "
                    f"| weekly_hours_if_added={projected_hours:.1f} "
                    f"| {day or 'same-day'} assignments={assignment_text}{loc_note} "
                    f"| matching_track_record={track_record}"
                )
            )
        if candidate_lines:
            lines.append("Potential trainer evidence (not a final recommendation; use with schedule conflicts and history):")
            for _, _, line in sorted(candidate_lines, key=lambda item: (item[0], item[1]))[:18]:
                lines.append(line)

    return "\n".join(lines)


def build_chat_context(
    schedule_path: Path,
    scorecard_path: Path,
    profiles_path: Path,
    user_message: str = "",
) -> str:
    schedule_data = _load_json(schedule_path, {})
    scorecard = _load_json(scorecard_path, {})
    profiles = _load_json(profiles_path, [])

    location_counts = [
        f"{loc}: {len(rows or [])} scheduled classes"
        for loc, rows in (schedule_data.get("locations") or {}).items()
    ]
    score_parts = []
    for loc, data in (scorecard.get("locations") or {}).items():
        fill = data.get("predicted_avg_fill_rate") or data.get("avg_fill_rate") or 0
        total = data.get("total_classes") or 0
        score_parts.append(f"{loc}: {total} classes, {fill:.0%} fill")

    trainer_lines = []
    for profile in profiles[:80]:
        name = profile.get("name", "")
        tier = profile.get("tier", 3)
        active = profile.get("active", True)
        locs = ", ".join((profile.get("locations") or {}).keys())
        quals = ", ".join(key for key, enabled in (profile.get("qualifications") or {}).items() if enabled)
        trainer_lines.append(f"  [T{tier}] {name} | active={active} | locations: {locs} | quals: {quals}")

    evidence = _build_relevant_evidence(user_message, schedule_data, profiles)

    return (
        "You are an expert Physique 57 India studio schedule assistant. "
        "Always answer the user's question directly using the LLM. Be concise, data-driven, and practical. "
        "Do not use markdown tables unless the user explicitly asks for a table. "
        "Give actual suggestions with reasons, using the evidence below. "
        "For substitution questions, name specific candidates and explain schedule conflicts, same-shift/location fit, qualifications, load, and historical metrics when available. "
        "Use the ranked candidate evidence when present: recommend eligible trainers first and explicitly reject blocked trainers with their blocked_reasons. "
        "If the evidence is insufficient, say what is missing instead of inventing a policy or metric. "
        "Do not claim a studio-, trainer-, or class-specific rule exists unless it appears in the enabled settings or the user supplied it.\n\n"
        "STRUCTURED RECOMMENDATION FORMAT:\n"
        "Best option: <specific trainer/class/action, or 'No fully compliant option found'>\n"
        "Avoid: <blocked or weak options and the concrete reason>\n"
        "Why: <2-4 evidence-backed bullets or short sentences using schedule facts>\n"
        "Next action: <one concrete operational step>\n\n"
        f"CURRENT SCHEDULE: {'; '.join(location_counts) if location_counts else 'No schedule loaded'}\n"
        + (f"SCORECARD: {'; '.join(score_parts)}\n" if score_parts else "")
        + (f"\n{evidence}\n" if evidence else "")
        + (f"\nTRAINERS:\n" + "\n".join(trainer_lines) + "\n" if trainer_lines else "")
        + "\nDefault rules are limited to universal scheduling guardrails. "
        "Studio, trainer, and class-specific rules must come from user-created saved rules in Settings."
    )
