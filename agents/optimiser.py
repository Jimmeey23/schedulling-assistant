import json
import random
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, List, Dict, Set, Tuple
from datetime import date, timedelta
from collections import defaultdict

STATE_DIR = Path("state")
RULES_DIR = Path("rules")
CONFIG_DIR = Path("config")

DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DOW_MAP = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}
DOW_REVERSE = {v: k for k, v in DOW_MAP.items()}

TIER1_WEEKLY_TARGET_MIN = 900  # 15 hours
MIN_CLASS_START_MIN = 7 * 60
BLOCKED_MIDDAY_START_MIN = 13 * 60
BLOCKED_MIDDAY_END_MIN = 15 * 60
MAX_CLASS_START_MIN = 20 * 60 + 30

# Accurate class durations in minutes
CLASS_DURATIONS: Dict[str, int] = {
    "Studio Barre 57": 57,
    "Studio Barre 57 Express": 45,
    "Studio Cardio Barre": 57,
    "Studio Mat 57": 57,
    "Studio Back Body Blaze": 57,
    "Studio FIT": 57,
    "Studio Power Barre": 57,
    "Studio Barre Fusion": 57,
    "Studio Amped Up!": 57,
    "Studio HIIT": 45,
    "Studio Recovery": 30,
    "Studio Foundations": 57,
    "Studio Dance Cardio": 45,
    "Studio Flex & Flow": 45,
    "Studio Strength Lab": 57,
    "Studio PowerCycle": 45,
    "Studio PowerCycle Express": 30,
    "Studio Hosted Class": 60,
    "Pre/Post Natal": 57,
}

def get_class_duration(class_name: str) -> int:
    if class_name in CLASS_DURATIONS:
        return CLASS_DURATIONS[class_name]
    # Fuzzy match
    lower = class_name.lower()
    if "express" in lower and "cycle" in lower:
        return 30
    if "cycle" in lower:
        return 45
    if "recovery" in lower:
        return 30
    if "express" in lower:
        return 45
    return 57

# Room model per location
# Each room can hold exactly 1 class at a time
LOCATION_ROOMS: Dict[str, Dict[str, dict]] = {
    "Kwality House, Kemps Corner": {
        "strength_lab": {"capacity": 7, "families": ["strength_lab"]},
        "powercycle":   {"capacity": 10, "families": ["powercycle"]},
        "studio_a":     {"capacity": 22, "families": None},
        "studio_b":     {"capacity": 13, "families": None},
    },
    "Supreme HQ, Bandra": {
        "powercycle": {"capacity": 14, "families": ["powercycle"]},
        "studio_a":   {"capacity": 14, "families": None},
        "studio_b":   {"capacity": 14, "families": None},
    },
    "Kenkere House": {
        "studio_a": {"capacity": 15, "families": None},
        "studio_b": {"capacity": 13, "families": None},
    },
}

# Prime time windows (minutes since midnight)
PRIME_AM_START = 8 * 60       # 08:00
PRIME_AM_END   = 11 * 60 + 30 # 11:30
PRIME_PM_START = 17 * 60 + 30 # 17:30
PRIME_PM_END   = 19 * 60 + 30 # 19:30

# Classes never auto-scheduled
AUTO_EXCLUDE_KEYWORDS = {
    "sweat in 30",
    "dance recovery",
    "pre/post natal",
    "hosted class",
    "foundations",
    "unknown class",
}
AUTO_EXCLUDE_FAMILIES = {"special", "prenatal"}

# Prime AM and PM slot sets (for priority ordering)
PRIME_AM_SLOTS = {"08:30", "09:00", "09:30", "10:15", "11:00", "11:30"}
PRIME_PM_SLOTS = {"17:45", "18:00", "18:15", "19:00", "19:15", "19:30"}

# Experimental quota: 25% of non-pinned classes per day should be varied for schedule diversity
EXPERIMENTAL_RATIO = 0.25

# Data-driven daily class count ranges derived from historical p25/p75 per location per day.
# Each iteration seed picks a target within [min, max]; v1=lower, v2=middle, v3=upper.
DATA_DRIVEN_DAILY_RANGES: Dict[str, Dict[str, tuple]] = {
    "Kwality House, Kemps Corner": {
        "Monday": (12, 13), "Tuesday": (10, 12), "Wednesday": (10, 13),
        "Thursday": (11, 13), "Friday": (10, 11), "Saturday": (11, 12), "Sunday": (6, 6),
    },
    "Supreme HQ, Bandra": {
        "Monday": (8, 10), "Tuesday": (9, 10), "Wednesday": (8, 10),
        "Thursday": (9, 11), "Friday": (9, 11), "Saturday": (11, 12), "Sunday": (6, 6),
    },
    "Kenkere House": {
        "Monday": (8, 9), "Tuesday": (7, 9), "Wednesday": (8, 9),
        "Thursday": (7, 8), "Friday": (7, 8), "Saturday": (8, 11), "Sunday": (5, 6),
    },
}

# Maximum times a class format can appear in a single day per location.
# Barre 57 is a pillar so it gets 3; specialty formats get 1.
MAX_FORMAT_PER_DAY: Dict[str, int] = {
    "Studio Barre 57": 3,
    "Studio Cardio Barre": 2,
    "Studio Mat 57": 1,
    "Studio Back Body Blaze": 1,        # Reduced from 2 to limit weekly total to 3-4
    "Studio FIT": 1,
    "Studio Recovery": 1,
    "Studio Foundations": 2,
    "Studio Amped Up!": 1,
    "Studio HIIT": 1,
    "Studio SWEAT In 30": 1,
    "Studio PowerCycle": 3,
    "Studio PowerCycle Express": 1,
    "Studio Barre 57 Express": 1,
    "Studio Cardio Barre Express": 1,
    "Studio Cardio Barre Plus": 1,
    "Studio Back Body Blaze Express": 1,
    "Studio Mat 57 Express": 1,
    "Studio Strength Lab": 2,
    "Studio Strength Lab (Push)": 2,
    "Studio Strength Lab (Pull)": 2,
    "Studio Strength Lab (Full Body)": 2,
    "Studio Trainer's Choice": 1,
    "Studio Power Barre": 1,
    "Studio Dance Cardio": 1,
}
DEFAULT_MAX_FORMAT_PER_DAY = 1

# How many of each format we want to ensure appear across the WEEK per location.
# Formats in this dict are boosted when they haven't hit their weekly minimum yet.
WEEKLY_FORMAT_MINIMUMS: Dict[str, int] = {
    "Studio FIT": 6,               # Popular format - boost to ~1/day
    "Studio Mat 57": 5,            # Popular format - increase
    "Studio Barre 57": 12,         # Most popular - ensure strong presence
    "Studio Cardio Barre": 7,      # Increased for Kwality - popular format
    "Studio PowerCycle": 10,       # Strong floor — dedicated room, major pillar at both Kwality + Supreme
    "Studio Strength Lab": 6,      # increased Kwality floor in the dedicated room
    "Studio Foundations": 2,       # Reduce - now banned anyway
    "Studio Back Body Blaze": 0,   # Let schedule naturally 3-4 times (daily cap = 1)
    "Studio Recovery": 1,          # Weekend only
    "Studio Amped Up!": 1,         # REDUCE - too many currently
    "Studio HIIT": 1,              # REDUCE - too many currently
    "Studio SWEAT In 30": 0,       # Banned
}

# Format popularity scoring - boost scores for popular formats, penalize unpopular
FORMAT_POPULARITY_BONUS: Dict[str, float] = {
    "Studio Barre 57": 8.0,
    "Studio Mat 57": 6.0,
    "Studio FIT": 6.0,
    "Studio Cardio Barre": 5.0,
    "Studio PowerCycle": 5.0,
    "Studio Strength Lab": 10.0,
    "Studio HIIT": -10.0,          # PENALIZE - reduce frequency
    "Studio Amped Up!": -10.0,     # PENALIZE - reduce frequency
    "Studio Dance Recovery": -15.0,
}

# Diversity score adjustments: first-of-format-today gets a bonus, repeats get penalties
DIVERSITY_BONUS_FIRST = 18.0   # first occurrence of a format today
DIVERSITY_BONUS_SECOND = 5.0   # second (only for multi-cap formats like Barre 57)
DIVERSITY_PENALTY_OVER_CAP = 999.0  # effectively blocks it

# Per-class hard scheduling constraints (day/time guards applied inside _build_candidates)
# Each entry: (allowed_days, earliest_start_min, latest_start_min)
# allowed_days: None = all days; set = restrict to those days
CLASS_SCHEDULE_GUARDS: Dict[str, dict] = {}


def slot_time_to_minutes(t: str) -> int:
    parts = str(t).split(":")
    return int(parts[0]) * 60 + int(parts[1])


def time_windows_overlap(start1: int, dur1: int, start2: int, dur2: int) -> bool:
    """Returns True if two [start, start+dur) windows overlap."""
    return start1 < (start2 + dur2) and start2 < (start1 + dur1)


def is_prime_slot(time_str: str) -> bool:
    m = slot_time_to_minutes(time_str)
    return (PRIME_AM_START <= m <= PRIME_AM_END) or (PRIME_PM_START <= m <= PRIME_PM_END)


def is_am_slot(time_str: str) -> bool:
    return slot_time_to_minutes(time_str) < slot_time_to_minutes("13:00")


def shift_label(time_str: str) -> str:
    return "AM" if is_am_slot(time_str) else "PM"


def canonical_class_key(class_name: str) -> str:
    lower = (class_name or "").lower()
    if "strength lab" in lower:
        return "Studio Strength Lab"
    if "back body" in lower:
        return "Studio Back Body Blaze"
    if "powercycle" in lower or "power cycle" in lower:
        return "Studio PowerCycle"
    if "hiit" in lower:
        return "Studio HIIT"
    if "amped" in lower:
        return "Studio Amped Up!"
    if "cardio barre" in lower:
        return "Studio Cardio Barre"
    if "mat 57" in lower:
        return "Studio Mat 57"
    if "barre 57" in lower or "power barre" in lower or "barre fusion" in lower:
        return "Studio Barre 57"
    if "recovery" in lower or "flex" in lower:
        return "Studio Recovery"
    if "fit" in lower:
        return "Studio FIT"
    return class_name


def normalize_trainer_name(name: str) -> str:
    return " ".join(str(name or "").split())


def qualification_key_for_class(class_name: str) -> str:
    lower = (class_name or "").lower()
    if "powercycle" in lower or "power cycle" in lower:
        return "powercycle"
    if "strength lab" in lower:
        return "strength_lab"
    if "mat 57" in lower:
        return "mat_57"
    if "foundations" in lower:
        return "foundations"
    if "pre/post" in lower or "pre post" in lower or "natal" in lower:
        return "pre_post_natal"
    if "amped" in lower:
        return "amped_up"
    if "hiit" in lower:
        return "hiit"
    if "recovery" in lower:
        return "recovery"
    return "all_barre"


def time_band(time_str: str) -> str:
    h = int(str(time_str)[:2])
    if 7 <= h <= 9:
        return "morning"
    elif 10 <= h <= 12:
        return "midday"
    elif 13 <= h <= 16:
        return "afternoon"
    return "evening"


def get_class_format(class_name: str) -> str:
    """Coarse format label used for consecutive-duplicate check."""
    lower = class_name.lower()
    if "barre 57" in lower or "cardio barre" in lower or "power barre" in lower or "barre fusion" in lower:
        return "barre_family"
    if "powercycle" in lower or "power cycle" in lower:
        return "powercycle"
    if "strength" in lower:
        return "strength_lab"
    if "mat 57" in lower:
        return "mat_57"
    if "back body blaze" in lower:
        return "back_body_blaze"
    if "recovery" in lower or "flex" in lower:
        return "recovery"
    if "foundations" in lower:
        return "foundations"
    if "fit" in lower:
        return "fit"
    if "amped" in lower:
        return "amped_up"
    if "hiit" in lower:
        return "hiit"
    return lower.replace(" ", "_")


def is_excluded_class(class_name: str, family: str) -> bool:
    if family in AUTO_EXCLUDE_FAMILIES:
        return True
    class_name_lower = class_name.lower()
    return any(kw in class_name_lower for kw in AUTO_EXCLUDE_KEYWORDS)


def slot_is_in_blocked_window(day_name: str, time_str: str) -> bool:
    start_min = slot_time_to_minutes(time_str)
    if start_min < MIN_CLASS_START_MIN or start_min > MAX_CLASS_START_MIN:
        return True
    if BLOCKED_MIDDAY_START_MIN <= start_min < BLOCKED_MIDDAY_END_MIN:
        return True
    if day_name == "Sunday" and (start_min < slot_time_to_minutes("10:00") or time_band(time_str) == "evening"):
        return True
    return False


def shift3_label(time_str: str) -> str:
    """3-shift model: morning (07:00-11:59), midday (12:00-15:59), evening (16:00-20:59)."""
    m = slot_time_to_minutes(time_str)
    if m < slot_time_to_minutes("12:00"):
        return "morning"
    if m < slot_time_to_minutes("16:00"):
        return "midday"
    return "evening"


def is_recovery_allowed_in_slot(time_str: str) -> bool:
    """Legacy (kept for back-compat). Recovery placement is now validated dynamically
    against same-shift slots; this just blocks the very earliest slots."""
    return slot_time_to_minutes(time_str) >= slot_time_to_minutes("10:30")


def is_recovery_last_in_shift(class_name: str, time_str: str, slots_today: List) -> bool:
    """Recovery must be the LAST session of its shift at this location.
    3-shift model: morning / midday / evening."""
    if "Recovery" not in class_name:
        return True

    current_shift = shift3_label(time_str)
    current_min = slot_time_to_minutes(time_str)

    for s in slots_today:
        if shift3_label(s.time) != current_shift:
            continue
        if slot_time_to_minutes(s.time) > current_min:
            return False  # Another class is later in the same shift
    return True


def would_block_recovery(class_name: str, time_str: str, slots_today: List) -> bool:
    """If we are placing a non-Recovery class at time_str and a Recovery is already
    scheduled earlier in the same shift, this placement would push Recovery out of
    last-position — block it."""
    if "Recovery" in class_name:
        return False
    current_shift = shift3_label(time_str)
    current_min = slot_time_to_minutes(time_str)
    for s in slots_today:
        if "Recovery" not in s.class_name:
            continue
        if shift3_label(s.time) != current_shift:
            continue
        if slot_time_to_minutes(s.time) < current_min:
            return True
    return False


def build_constraint_violations(location: str, day_name: str, time_str: str,
                                class_name: str, slots_today: List["ScheduleSlot"]) -> List[str]:
    violations: List[str] = []
    if is_excluded_class(class_name, ""):
        violations.append("UNIV-021: Banned class format")
    if slot_is_in_blocked_window(day_name, time_str):
        violations.append("UNIV-024: Blocked time window")
    if "PowerCycle" in class_name and location == "Kenkere House":
        violations.append("UNIV-011: PowerCycle at Kenkere")
    if "Strength Lab" in class_name and location != "Kwality House, Kemps Corner":
        violations.append("UNIV-012: Strength Lab not at Kwality")
    if "Recovery" in class_name:
        if slots_today and time_str <= min(s.time for s in slots_today):
            violations.append("UNIV-007: Recovery is first class")
        if not is_recovery_last_in_shift(class_name, time_str, slots_today):
            violations.append("UNIV-026: Recovery not last in shift")
        if not is_recovery_allowed_in_slot(time_str):
            violations.append("UNIV-026: Recovery in early slot (must be after 11:30 AM or 18:45 PM)")
    if "Foundations" in class_name and time_str in ("11:30", "19:15"):
        violations.append("UNIV-008: Foundations at forbidden slot")
    current_min = slot_time_to_minutes(time_str)
    current_format = get_class_format(class_name)
    earlier = [s for s in slots_today if slot_time_to_minutes(s.time) < current_min]
    later = [s for s in slots_today if slot_time_to_minutes(s.time) > current_min]
    neighbours = []
    if earlier:
        neighbours.append(max(earlier, key=lambda s: slot_time_to_minutes(s.time)))
    if later:
        neighbours.append(min(later, key=lambda s: slot_time_to_minutes(s.time)))
    if any(get_class_format(s.class_name) == current_format for s in neighbours):
        violations.append("UNIV-023: Consecutive class format")
    return violations


@dataclass
class ScheduleSlot:
    location: str
    date: str
    day_of_week: str
    time: str
    class_name: str
    trainer_1: str
    trainer_2: str
    cover: str                   # empty unless explicitly needed
    room: str                    # room_id e.g. "studio_a", "powercycle"
    capacity: int
    duration_min: int
    predicted_fill_rate: float
    score: float
    recommendation: str
    is_experimental: bool
    scheduling_reason: str
    historical_avg_fill: float
    historical_avg_checkin: float
    historical_session_count: int
    historical_late_cancel_rate: float = 0.0
    historical_no_show_rate: float = 0.0
    score_breakdown: dict = field(default_factory=dict)
    constraint_violations: List[str] = field(default_factory=list)


class TrainerState:
    """Tracks a trainer's load across the full scheduling week at all locations."""

    def __init__(self, name: str, tier: int):
        self.name = name
        self.tier = tier
        self.weekly_minutes: int = 0
        # day -> [(time_str, location, class_name)]
        self._schedule: Dict[str, List[Tuple[str, str, str]]] = {}

    def classes_today(self, day: str, location: str) -> List[str]:
        return [t for t, loc, _ in self._schedule.get(day, []) if loc == location]

    def worked_days(self) -> Set[str]:
        return set(self._schedule.keys())

    def worked_days_count(self) -> int:
        return len(self.worked_days())

    def locations_in_shift(self, day: str, shift: str) -> Set[str]:
        return {loc for t, loc, _ in self._schedule.get(day, []) if shift_label(t) == shift}

    def shifts_worked_today(self, day: str) -> Set[str]:
        return {shift_label(t) for t, _, _ in self._schedule.get(day, [])}

    def can_add(self, day: str, time_str: str, location: str, class_name: str,
                max_per_day: int, win_start: str, win_end: str) -> bool:
        new_start = slot_time_to_minutes(time_str)
        new_dur = get_class_duration(class_name)
        candidate_shift = shift_label(time_str)

        # Time window check
        if new_start < slot_time_to_minutes(win_start):
            return False
        if new_start > slot_time_to_minutes(win_end):
            return False

        # Max per day at this location
        today_loc = self.classes_today(day, location)
        if len(today_loc) >= max_per_day:
            return False

        # Duration-based overlap check against ALL classes today (any location)
        for (et, eloc, ecls) in self._schedule.get(day, []):
            e_start = slot_time_to_minutes(et)
            e_dur = get_class_duration(ecls)
            if time_windows_overlap(new_start, new_dur, e_start, e_dur):
                return False

        shift_locations = self.locations_in_shift(day, candidate_shift)
        if shift_locations and location not in shift_locations:
            return False

        # AM/PM exclusivity: trainer cannot work both AM and PM on the same day
        opposite_shift = "PM" if candidate_shift == "AM" else "AM"
        if opposite_shift in self.shifts_worked_today(day):
            return False

        return True

    def add(self, day: str, time_str: str, location: str, class_name: str):
        if day not in self._schedule:
            self._schedule[day] = []
        self._schedule[day].append((time_str, location, class_name))
        self.weekly_minutes += get_class_duration(class_name)

    def at_weekly_target(self) -> bool:
        return self.tier == 1 and self.weekly_minutes >= TIER1_WEEKLY_TARGET_MIN


class RoomOccupancy:
    """Tracks room usage at a single location for one week."""

    def __init__(self, rooms: Dict[str, dict]):
        self.rooms = rooms
        # (day, room_id) -> [(start_min, end_min, class_name, trainer)]
        self._occ: Dict[Tuple[str, str], List[Tuple[int, int, str, str]]] = {}

    def is_available(self, day: str, room_id: str, start_min: int, duration: int) -> bool:
        end_min = start_min + duration
        for (s, e, _, _) in self._occ.get((day, room_id), []):
            if start_min < e and s < end_min:
                return False
        return True

    def occupy(self, day: str, room_id: str, start_min: int, duration: int,
               class_name: str, trainer: str):
        key = (day, room_id)
        if key not in self._occ:
            self._occ[key] = []
        self._occ[key].append((start_min, start_min + duration, class_name, trainer))

    def last_class_in_room(self, day: str, room_id: str) -> Optional[str]:
        entries = self._occ.get((day, room_id), [])
        if not entries:
            return None
        return max(entries, key=lambda x: x[1])[2]  # class_name of latest entry

    def find_room(self, day: str, class_family: str, start_min: int, duration: int) -> Optional[str]:
        """Return a suitable free room for this class family, or None."""
        # Specialist rooms first
        if class_family == "strength_lab":
            if "strength_lab" in self.rooms and self.is_available(day, "strength_lab", start_min, duration):
                return "strength_lab"
            return None  # strength lab can only go in strength lab room
        if class_family == "powercycle":
            if "powercycle" in self.rooms and self.is_available(day, "powercycle", start_min, duration):
                return "powercycle"
            return None  # cycle only in cycle room
        # General: studio_a preferred (larger), then studio_b
        for room_id in ["studio_a", "studio_b"]:
            if room_id in self.rooms and self.is_available(day, room_id, start_min, duration):
                return room_id
        return None


class ScheduleOptimiser:
    def __init__(self, target_week_start: str, locations: List[str] = None,
                 overrides_path: Optional[str] = None,
                 variation_seed: int = 0,
                 output_suffix: str = "",
                 optimization_mode: str = "max_score"):
        valid_modes = {"max_score", "trainer_hours", "class_variety"}
        if optimization_mode not in valid_modes:
            raise ValueError(f"Unknown optimization_mode '{optimization_mode}'")
        self.target_week_start = target_week_start
        self.locations = locations or [
            "Kwality House, Kemps Corner",
            "Supreme HQ, Bandra",
            "Kenkere House",
        ]
        self.overrides = self._load_overrides(overrides_path)
        self.schedule_config = self._load_schedule_config()
        self.ai_brief: dict = {}
        self._ai_boost: Dict[tuple, float] = {}
        self._ai_penalty: Dict[tuple, float] = {}
        self._ai_mix_boost: Dict[tuple, float] = {}
        self._variation_seed = variation_seed
        self._output_suffix = output_suffix
        self._optimization_mode = optimization_mode
        self._rng = random.Random(variation_seed) if variation_seed else None

    # ------------------------------------------------------------------ #
    #  Overrides
    # ------------------------------------------------------------------ #

    def _load_overrides(self, path):
        candidates = [Path(path)] if path else []
        candidates.append(CONFIG_DIR / "trainer_overrides.json")
        candidates.append(CONFIG_DIR / "schedule_config.json")
        merged = {}
        list_keys = {
            "inactive_trainers",
            "leave_periods",
            "off_days",
            "tier_overrides",
            "location_preferences",
            "available_days_overrides",
            "time_window_overrides",
            "max_classes_overrides",
        }
        for p in candidates:
            if p.exists():
                with open(p) as f:
                    data = json.load(f)
                data = {k: v for k, v in data.items() if not k.startswith("_")}
                for key, value in data.items():
                    if key in list_keys:
                        current = merged.setdefault(key, [])
                        if isinstance(value, list):
                            current.extend(value)
                    elif key not in merged:
                        merged[key] = value
                    elif isinstance(value, dict) and isinstance(merged.get(key), dict):
                        merged[key].update(value)
                    else:
                        merged[key] = value
        if "inactive_trainers" in merged:
            merged["inactive_trainers"] = sorted(set(merged["inactive_trainers"]))
        return merged

    def _load_schedule_config(self):
        path = CONFIG_DIR / "schedule_config.json"
        if not path.exists():
            return {}
        with open(path) as f:
            return json.load(f)

    def _is_inactive(self, trainer):
        inactive = {normalize_trainer_name(t) for t in self.overrides.get("inactive_trainers", [])}
        inactive.update(getattr(self, "inactive_profile_trainers", set()))
        return normalize_trainer_name(trainer) in inactive

    def _on_leave(self, trainer, date_str, location=None):
        for p in self.overrides.get("leave_periods", []):
            if normalize_trainer_name(p["trainer"]) != normalize_trainer_name(trainer): continue
            if p.get("location") and p["location"] != location: continue
            if p["from_date"] <= date_str <= p["to_date"]: return True
        for o in self.overrides.get("off_days", []):
            if normalize_trainer_name(o["trainer"]) != normalize_trainer_name(trainer): continue
            if o.get("location") and o["location"] != location: continue
            if o["date"] == date_str: return True
        return False

    def _get_tier(self, trainer, base):
        for ov in self.overrides.get("tier_overrides", []):
            if normalize_trainer_name(ov["trainer"]) == normalize_trainer_name(trainer): return ov["tier"]
        return base

    def _loc_excluded(self, trainer, location):
        for p in self.overrides.get("location_preferences", []):
            if normalize_trainer_name(p["trainer"]) == normalize_trainer_name(trainer) and p["location"] == location:
                return p.get("preference") == "excluded"
        return False

    def _available_days(self, trainer, location, default):
        for ov in self.overrides.get("available_days_overrides", []):
            if normalize_trainer_name(ov["trainer"]) == normalize_trainer_name(trainer) and ov["location"] == location:
                return ov["days"]
        return default

    def _time_window(self, trainer, location, default_start, default_end):
        for ov in self.overrides.get("time_window_overrides", []):
            if normalize_trainer_name(ov["trainer"]) == normalize_trainer_name(trainer) and ov["location"] == location:
                return ov.get("start", default_start), ov.get("end", default_end)
        return default_start, default_end

    def _max_per_day(self, trainer, location, default):
        for ov in self.overrides.get("max_classes_overrides", []):
            if normalize_trainer_name(ov["trainer"]) == normalize_trainer_name(trainer) and (not ov.get("location") or ov["location"] == location):
                return ov.get("max_per_day", default)
        return default

    def _max_per_week(self, trainer, location):
        for ov in self.overrides.get("max_classes_overrides", []):
            if normalize_trainer_name(ov["trainer"]) == normalize_trainer_name(trainer) and (not ov.get("location") or ov["location"] == location):
                return ov.get("max_per_week")
        return None

    def _normalise_trainer_names(self, profiles_list: List[dict], scores_data: dict, metrics_data: dict):
        alias = {normalize_trainer_name(p.get("name")): p.get("name") for p in profiles_list if p.get("name")}
        for rows_key in ("class_slot_ranking", "slot_group_ranking"):
            for row in scores_data.get(rows_key, []):
                raw = row.get("trainer")
                if raw:
                    row["trainer"] = alias.get(normalize_trainer_name(raw), normalize_trainer_name(raw))
        for row in metrics_data.get("class_trainer_slot_metrics", []):
            raw = row.get("trainer")
            if raw:
                row["trainer"] = alias.get(normalize_trainer_name(raw), normalize_trainer_name(raw))

    def _enrich_trainer_profiles(self, profiles_list: List[dict], scores_data: dict):
        by_name = {p["name"]: p for p in profiles_list if p.get("name")}
        for p in profiles_list:
            p.setdefault("qualifications", {})
            for key in ("all_barre", "mat_57", "powercycle", "strength_lab", "foundations", "pre_post_natal", "amped_up", "hiit", "recovery"):
                p["qualifications"].setdefault(key, False)

        for row in scores_data.get("class_slot_ranking", []):
            trainer = row.get("trainer")
            if not trainer:
                continue
            if self._is_inactive(trainer):
                continue
            if trainer not in by_name:
                by_name[trainer] = {
                    "name": trainer,
                    "tier": 3,
                    "locations": {},
                    "qualifications": {
                        "all_barre": False, "mat_57": False, "powercycle": False,
                        "strength_lab": False, "foundations": False, "pre_post_natal": False,
                        "amped_up": False, "hiit": False, "recovery": False,
                    },
                }
                profiles_list.append(by_name[trainer])
            profile = by_name[trainer]
            q = profile.setdefault("qualifications", {})
            q[qualification_key_for_class(row.get("class", ""))] = True
            loc = row.get("location")
            if loc and loc not in profile.setdefault("locations", {}):
                profile["locations"][loc] = {
                    "available_days": DAY_ORDER[:6],
                    "time_window": {"start": "06:00", "end": "22:00"},
                    "max_classes_per_day": 4,
                    "session_count": int(row.get("session_count", 0) or 0),
                }

        for p in profiles_list:
            notes = " ".join((ld.get("notes", "") or "") for ld in (p.get("locations") or {}).values()).lower()
            q = p.setdefault("qualifications", {})
            if "powercycle" in notes or "power cycle" in notes:
                q["powercycle"] = True
            if "strength lab" in notes:
                q["strength_lab"] = True
            if not any(q.values()):
                q["all_barre"] = True

    # ------------------------------------------------------------------ #
    #  Main run
    # ------------------------------------------------------------------ #

    def run(self) -> dict:
        print("[Agent 5] Optimiser v3 starting (duration-aware, room-parallel, AM/PM balanced)...")

        with open(STATE_DIR / "02_metrics.json") as f:
            metrics_data = json.load(f)
        with open(STATE_DIR / "03_scores.json") as f:
            scores_data = json.load(f)
        with open(RULES_DIR / "trainer_profiles.json") as f:
            profiles_list = json.load(f)
        with open(RULES_DIR / "kwality_rules.json") as f:
            kwality_rules = json.load(f)
        with open(RULES_DIR / "supreme_rules.json") as f:
            supreme_rules = json.load(f)
        with open(RULES_DIR / "kenkere_rules.json") as f:
            kenkere_rules = json.load(f)
        with open(RULES_DIR / "class_formats.json") as f:
            class_formats_list = json.load(f)

        self._normalise_trainer_names(profiles_list, scores_data, metrics_data)
        self.inactive_profile_trainers = {
            normalize_trainer_name(p.get("name"))
            for p in profiles_list
            if p.get("name") and p.get("active") is False
        }
        self._enrich_trainer_profiles(profiles_list, scores_data)

        self.class_family: Dict[str, str] = {c["name"]: c.get("family", "") for c in class_formats_list}
        self.location_rules = {
            "Kwality House, Kemps Corner": kwality_rules,
            "Supreme HQ, Bandra": supreme_rules,
            "Kenkere House": kenkere_rules,
        }
        self.scores_data = scores_data
        self.slot_availability = metrics_data.get("slot_availability", {})

        # Load AI brief if available (produced by Agent 4.5)
        ai_brief_path = STATE_DIR / "04b_ai_brief.json"
        self.ai_brief: dict = {}
        if ai_brief_path.exists():
            with open(ai_brief_path) as f:
                self.ai_brief = json.load(f)
            n_boost = len(self.ai_brief.get("priority_hints", []))
            n_pen = len(self.ai_brief.get("avoid_hints", []))
            print(f"[Agent 5] AI brief loaded — {n_boost} priority hints, {n_pen} avoid hints")
            # Build fast lookup: (location, class_name, trainer, day) -> delta
            self._ai_boost: Dict[tuple, float] = {}
            self._ai_penalty: Dict[tuple, float] = {}
            for h in self.ai_brief.get("priority_hints", []):
                key = (h["location"], h["class_name"], h["trainer"], h["day"])
                self._ai_boost[key] = self._ai_boost.get(key, 0) + float(h.get("boost", 0))
            for h in self.ai_brief.get("avoid_hints", []):
                key = (h["location"], h["class_name"], h["trainer"], h["day"])
                self._ai_penalty[key] = self._ai_penalty.get(key, 0) + float(h.get("penalty", 0))
            # Class-level mix boosts: (location, class_name) -> delta
            self._ai_mix_boost: Dict[tuple, float] = {}
            for loc, classes in self.ai_brief.get("class_mix_boosts", {}).items():
                for cname, boost in classes.items():
                    self._ai_mix_boost[(loc, cname)] = float(boost)
        else:
            self._ai_boost = {}
            self._ai_penalty = {}
            self._ai_mix_boost = {}

        # Build historical lookup: (location, class, trainer, day, time_approx) -> metrics
        self.hist_lookup: Dict[Tuple, dict] = {}
        for m in metrics_data["class_trainer_slot_metrics"]:
            t = m["time"][:5] if m["time"] else ""
            key = (m["location"], m["class"], m["trainer"], m["day"], t)
            self.hist_lookup[key] = m

        # Trainer states (shared across locations — tracks weekly hours)
        raw_profiles = {
            p["name"]: p
            for p in profiles_list
            if p.get("name") and not self._is_inactive(p["name"])
        }
        self.trainer_profiles = raw_profiles
        self.trainer_states: Dict[str, TrainerState] = {}
        for name, p in raw_profiles.items():
            tier = self._get_tier(name, p.get("tier", 3))
            self.trainer_states[name] = TrainerState(name, tier)

        # PROTECT combos indexed by (location, day_int). Class/time locks come
        # from UniqueID1 slot groups; trainer choice is handled later from the
        # UniqueID2 trainer ranking.
        self.protected: Dict[Tuple[str, int], List[dict]] = {}
        self.protected_class_times: Dict[Tuple[str, int], List[dict]] = {}
        for r in scores_data.get("class_slot_ranking", []):
            cname = r["class"]
            fam = self.class_family.get(cname, "")
            if is_excluded_class(cname, fam):
                continue
            key = (r["location"], r["day"])
            if r.get("protect_exact_combo"):
                self.protected.setdefault(key, []).append(r)
        for r in scores_data.get("slot_group_ranking", []):
            cname = r["class"]
            fam = self.class_family.get(cname, "")
            if is_excluded_class(cname, fam):
                continue
            key = (r["location"], r["day"])
            if r.get("pinned_slot") or r.get("protect_class_time"):
                self.protected_class_times.setdefault(key, []).append(r)

        week_start = date.fromisoformat(self.target_week_start)
        all_slots: List[ScheduleSlot] = []

        for loc in self.locations:
            loc_slots = self._schedule_location(loc, week_start)
            all_slots.extend(loc_slots)

        self._print_utilisation()

        output = {
            "target_week_start": self.target_week_start,
            "schedule": [asdict(s) for s in all_slots],
            "optimization_mode": self._optimization_mode,
        }
        STATE_DIR.mkdir(exist_ok=True)
        suffix = f"_{self._output_suffix}" if self._output_suffix else ""
        with open(STATE_DIR / f"05_draft_schedule{suffix}.json", "w") as f:
            json.dump(output, f, indent=2)

        print(f"[Agent 5] Optimiser complete — {len(all_slots)} slots across {len(self.locations)} locations")
        return output

    def _pick_daily_target(self, location: str, day_name: str) -> int:
        """
        Return data-driven class count target for this location+day.
        AI brief overrides take precedence. Seed-based iteration picks within
        the historical p25/p75 range: seed=0 → lower, seed=42 → mid, seed=137 → upper.
        """
        # Persisted settings override (if provided from the Settings modal)
        config_targets = self.schedule_config.get("targets", {}).get(location, {}).get(day_name, {})
        if isinstance(config_targets, dict) and config_targets.get("target") is not None:
            return int(config_targets["target"])

        # AI brief override (if provided)
        ai_overrides = getattr(self, "ai_brief", {}).get("daily_target_overrides", {})
        loc_overrides = ai_overrides.get(location, {})
        if day_name in loc_overrides:
            return int(loc_overrides[day_name])

        loc_ranges = DATA_DRIVEN_DAILY_RANGES.get(location, {})
        if day_name not in loc_ranges:
            # Fallback to location rules if day not in range table
            return self.location_rules[location]["target_classes_per_day"].get(day_name, 7)

        lo, hi = loc_ranges[day_name]
        if lo == hi:
            return lo

        # Pick within range based on seed: 0→lo, 42→mid, 137→hi
        seed = self._variation_seed
        if seed == 0:
            return lo
        elif seed == 137:
            return hi
        else:
            return (lo + hi) // 2

    def _class_mix_entry(self, location: str, class_name: str) -> dict:
        mix = self.schedule_config.get("class_mix", {}).get(location, {})
        return mix.get(canonical_class_key(class_name), {})

    def _weekly_min(self, location: str, class_name: str, default: Optional[int] = None) -> int:
        entry = self._class_mix_entry(location, class_name)
        if isinstance(entry, dict) and entry.get("min") is not None:
            return int(entry["min"])
        if default is not None:
            return int(default)
        return int(WEEKLY_FORMAT_MINIMUMS.get(canonical_class_key(class_name), 0))

    def _weekly_max(self, location: str, class_name: str, default: int) -> int:
        entry = self._class_mix_entry(location, class_name)
        if isinstance(entry, dict) and entry.get("max") is not None:
            cap = int(entry["max"])
            floor = int(entry.get("min", 0) or 0)
            if cap >= floor:
                return cap
        return int(default)

    def _weekly_count(self, weekly_class_counts: Optional[Dict[str, int]], slots_today: List[ScheduleSlot], class_name: str) -> int:
        key = canonical_class_key(class_name)
        wcc = weekly_class_counts or {}
        week_count = sum(int(v) for k, v in wcc.items() if canonical_class_key(k) == key)
        today_count = sum(1 for s in slots_today if canonical_class_key(s.class_name) == key)
        return week_count + today_count

    def _get_ai_hint_delta(self, location: str, class_name: str, trainer: str, day: int) -> float:
        """Return net AI score adjustment (boost minus penalty) for this combo."""
        key = (location, class_name, trainer, day)
        boost = self._ai_boost.get(key, 0.0)
        penalty = self._ai_penalty.get(key, 0.0)
        mix_boost = self._ai_mix_boost.get((location, class_name), 0.0)
        return boost + mix_boost - penalty

    def _score_noise(self, recommendation: str) -> float:
        """Add controlled randomness for schedule variation. Pinned/PROTECT_EXACT get minimal noise."""
        if self._rng is None:
            return 0.0
        if recommendation in ("PINNED", "PROTECT_EXACT"):
            return self._rng.gauss(0, 3.0)
        return self._rng.gauss(0, 13.0)

    def _evidence_adjusted_fill(self, hist: dict, fallback: float = 0.20) -> float:
        """Return a conservative fill estimate for scheduled output.

        Exact historical evidence should drive projected attendance. Unknown
        combinations are deliberately capped low so scorecards do not inflate
        schedule quality with optimistic defaults.
        """
        if hist and hist.get("session_count", 0) > 0:
            return hist.get("avg_fill_rate", fallback)
        return fallback

    def _print_utilisation(self):
        t1 = [(n, s) for n, s in self.trainer_states.items() if s.tier == 1 and s.weekly_minutes > 0]
        t1.sort(key=lambda x: -x[1].weekly_minutes)
        print("  Tier 1 weekly utilisation:")
        for name, st in t1:
            pct = min(100, int(st.weekly_minutes / TIER1_WEEKLY_TARGET_MIN * 100))
            print(f"    {name:<26} {st.weekly_minutes/60:4.1f}h  ({pct}% of 15h target)")

    # ------------------------------------------------------------------ #
    #  Location-level scheduling
    # ------------------------------------------------------------------ #

    def _get_viable_slots(self, location: str) -> Tuple[List[str], List[str]]:
        avail = self.slot_availability.get(location, [])
        viable = {
            s["time"][:5]
            for s in avail
            if s["viable"] and not slot_is_in_blocked_window("Monday", s["time"][:5])
        }

        am = sorted(t for t in viable if is_am_slot(t))
        pm = sorted(t for t in viable if not is_am_slot(t))

        def am_key(t): return (0 if t in PRIME_AM_SLOTS else 1, t)
        def pm_key(t): return (0 if t in PRIME_PM_SLOTS else 1, t)
        am.sort(key=am_key)
        pm.sort(key=pm_key)
        return am, pm

    def _schedule_location(self, location: str, week_start: date) -> List[ScheduleSlot]:
        loc_rules = self.location_rules[location]
        am_slots, pm_slots = self._get_viable_slots(location)
        rooms = LOCATION_ROOMS.get(location, {})
        room_occ = RoomOccupancy(rooms)

        print(f"  [{location}] AM: {am_slots}")
        print(f"  [{location}] PM: {pm_slots}")

        # Weekly class counters for enforcing per-week limits
        # strength_lab: max 2 per week at Kwality; powercycle: tracked for balance
        weekly_class_counts: Dict[str, int] = {}

        all_slots: List[ScheduleSlot] = []
        for day_idx, day_name in enumerate(DAY_ORDER):
            slot_date = week_start + timedelta(days=day_idx)
            date_str = slot_date.isoformat()
            target = self._pick_daily_target(location, day_name)
            day_slots = self._schedule_day(location, day_name, date_str, target,
                                           am_slots, pm_slots, room_occ,
                                           weekly_class_counts)
            all_slots.extend(day_slots)
            # Update weekly counts from today's schedule
            for s in day_slots:
                key = s.class_name.split("(")[0].strip()  # normalize variants
                weekly_class_counts[key] = weekly_class_counts.get(key, 0) + 1

        self._weekly_total_top_up(location, week_start, all_slots, room_occ, weekly_class_counts, am_slots, pm_slots)

        # Post-pass: horizontal column diversity (same-time across week)
        self._horizontal_diversity_pass(location, all_slots)

        # Post-pass: format floor fixup
        self._format_floor_fixup(location, all_slots, weekly_class_counts)

        return all_slots

    def _weekly_total_top_up(self, location: str, week_start: date, all_slots: List[ScheduleSlot],
                             room_occ: RoomOccupancy, weekly_class_counts: Dict[str, int],
                             am_slots: List[str], pm_slots: List[str]):
        weekly_min = {
            "Kwality House, Kemps Corner": 70,
            "Supreme HQ, Bandra": 60,
            "Kenkere House": 50,
        }.get(location, 0)
        if len(all_slots) >= weekly_min:
            return

        config_targets = self.schedule_config.get("targets", {}).get(location, {})
        attempts = 0
        while len(all_slots) < weekly_min and attempts < 250:
            attempts += 1
            by_day: Dict[str, List[ScheduleSlot]] = {day: [] for day in DAY_ORDER}
            for slot in all_slots:
                by_day.setdefault(slot.day_of_week, []).append(slot)
            placed = False
            for day_name in sorted(DAY_ORDER, key=lambda d: len(by_day.get(d, []))):
                day_idx = DOW_REVERSE[day_name]
                date_str = (week_start + timedelta(days=day_idx)).isoformat()
                day_max = int((config_targets.get(day_name) or {}).get("max", self._pick_daily_target(location, day_name) + 2))
                if len(by_day.get(day_name, [])) >= day_max:
                    continue
                used_at_time: Dict[str, Set[str]] = {}
                shift_trainers: Dict[str, List[str]] = {"AM": [], "PM": []}
                class_format_count_today: Dict[str, int] = {}
                for slot in by_day.get(day_name, []):
                    used_at_time.setdefault(slot.time, set()).add(slot.trainer_1)
                    shift = "AM" if is_am_slot(slot.time) else "PM"
                    if slot.trainer_1 not in shift_trainers[shift]:
                        shift_trainers[shift].append(slot.trainer_1)
                    class_format_count_today[slot.class_name] = class_format_count_today.get(slot.class_name, 0) + 1

                reserve_slots = [
                    "06:30", "07:00", "07:45", "08:15", "08:45",
                    "12:00", "12:30", "13:00", "13:30", "16:00",
                    "16:30", "20:00", "20:15",
                ]
                candidate_times = list(dict.fromkeys(pm_slots + am_slots + reserve_slots))
                for t in candidate_times:
                    if slot_is_in_blocked_window(day_name, t):
                        continue
                    result = self._fill_slot(
                        location, day_name, date_str, t, used_at_time, shift_trainers,
                        room_occ, by_day.get(day_name, []), 0, len(by_day.get(day_name, [])),
                        is_prime=is_prime_slot(t), weekly_class_counts=weekly_class_counts,
                        class_format_count_today=class_format_count_today,
                    )
                    if not result:
                        continue
                    room_occ.occupy(day_name, result.room, slot_time_to_minutes(result.time), result.duration_min, result.class_name, result.trainer_1)
                    self.trainer_states[result.trainer_1].add(day_name, result.time, location, result.class_name)
                    all_slots.append(result)
                    key = result.class_name.split("(")[0].strip()
                    weekly_class_counts[key] = weekly_class_counts.get(key, 0) + 1
                    placed = True
                    break
                if placed:
                    break
            if not placed:
                print(f"  [TOP-UP WARN] {location}: could not reach weekly floor {weekly_min}; stopped at {len(all_slots)}")
                return

    def _horizontal_diversity_pass(self, location: str, all_slots: List["ScheduleSlot"]):
        """If the same class appears > 4× in the same timeslot column across the week,
        flag and try to swap the LOWEST-scoring duplicates with a different class.
        Currently a soft pass: we mark the violation in scheduling_reason but only
        actually swap where a clean alternative is trivially available (different
        class with same trainer & higher diversity benefit). Full swap requires
        re-running constraint checks; for now we annotate."""
        from collections import defaultdict as _dd
        col = _dd(list)
        for s in all_slots:
            col[s.time].append(s)
        for time_str, slots in col.items():
            class_counts = _dd(list)
            for s in slots:
                class_counts[s.class_name].append(s)
            for cname, dup_list in class_counts.items():
                if len(dup_list) > 4:
                    # Pick lowest-scoring non-pinned, non-PROTECT_EXACT to annotate
                    sortable = sorted(dup_list, key=lambda s: s.score)
                    annotated = 0
                    for s in sortable:
                        if annotated >= len(dup_list) - 4:
                            break
                        if s.recommendation in ("PINNED", "PROTECT_EXACT"):
                            continue
                        s.scheduling_reason = (
                            f"[DIVERSITY-FLAG: {cname} appears {len(dup_list)}× at {time_str}] "
                            + s.scheduling_reason
                        )
                        annotated += 1

    def _format_floor_fixup(self, location: str, all_slots: List["ScheduleSlot"], weekly_class_counts: Dict[str, int]):
        """Annotate (and where possible address) format floors that aren't met.
        Currently logs warnings; aggressive swap-in would require full re-validation."""
        floors = {
            "Studio Mat 57": 4,
            "Studio FIT": 5,
        }
        if location != "Kenkere House":
            floors["Studio Cardio Barre"] = 4
        if location == "Kwality House, Kemps Corner":
            floors["Studio PowerCycle"] = 6
        elif location == "Supreme HQ, Bandra":
            floors["Studio PowerCycle"] = 10
        # Barre 57 family >= 14
        family_kw = ["Barre 57", "Cardio Barre", "Power Barre", "Barre Fusion"]
        bf_count = sum(1 for s in all_slots if any(kw in s.class_name for kw in family_kw))
        if bf_count < 14:
            print(f"  [FLOOR WARN] {location}: Barre family count {bf_count} < 14")

        for cname, floor in floors.items():
            cnt = sum(1 for s in all_slots if s.class_name == cname)
            if cnt < floor:
                print(f"  [FLOOR WARN] {location}: {cname} count {cnt} < floor {floor}")

    # ------------------------------------------------------------------ #
    #  Day-level scheduling
    # ------------------------------------------------------------------ #

    def _schedule_day(self, location, day_name, date_str, target_count,
                      am_slots, pm_slots, room_occ: RoomOccupancy,
                      weekly_class_counts: Optional[Dict[str, int]] = None) -> List[ScheduleSlot]:
        is_sunday = day_name == "Sunday"
        slots_today: List[ScheduleSlot] = []
        used_at_time: Dict[str, Set[str]] = {}
        shift_trainers: Dict[str, List[str]] = {"AM": [], "PM": []}

        # Track how many times each class format has been used today
        class_format_count_today: Dict[str, int] = {}

        exp_today = 0
        opt_today = 0

        def _register_slot(slot: ScheduleSlot, t: str):
            """Shared bookkeeping after a slot is confirmed."""
            room_occ.occupy(day_name, slot.room,
                            slot_time_to_minutes(slot.time), slot.duration_min,
                            slot.class_name, slot.trainer_1)
            self.trainer_states[slot.trainer_1].add(day_name, slot.time, location, slot.class_name)
            used_at_time.setdefault(t, set()).add(slot.trainer_1)
            shift = "AM" if is_am_slot(slot.time) else "PM"
            if slot.trainer_1 not in shift_trainers[shift]:
                shift_trainers[shift].append(slot.trainer_1)
            class_format_count_today[slot.class_name] = (
                class_format_count_today.get(slot.class_name, 0) + 1
            )

        # ---- Phase 1: Pinned rule-blocks ----
        pinned_times: Set[str] = set()
        wcc_for_caps = weekly_class_counts or {}
        for p in self._get_pinned_slots(location, day_name):
            t = p["time"]
            if slot_is_in_blocked_window(day_name, t):
                continue
            trainer = p["trainer"]
            cname = p["class"]
            if is_excluded_class(cname, self.class_family.get(cname, "")):
                continue
            # Hard weekly caps apply to pinned slots too
            if "Strength Lab" in cname:
                if location != "Kwality House, Kemps Corner":
                    continue
                if self._weekly_count(wcc_for_caps, slots_today, cname) >= self._weekly_max(location, cname, 8):
                    continue
            if "HIIT" in cname and self._weekly_count(wcc_for_caps, slots_today, cname) >= self._weekly_max(location, cname, 3):
                continue
            if "Amped Up" in cname and self._weekly_count(wcc_for_caps, slots_today, cname) >= self._weekly_max(location, cname, 2):
                continue
            if "Back Body" in cname and self._weekly_count(wcc_for_caps, slots_today, cname) >= self._weekly_max(location, cname, 3):
                continue
            if self._on_leave(trainer, date_str, location):
                continue
            # Check consecutive format constraint for pinned slots
            if self._would_repeat_consecutive_format(slots_today, t, cname):
                print(f"  WARNING: Skipping pinned {cname} at {t} due to consecutive format rule")
                continue
            dur = get_class_duration(cname)
            start_min = slot_time_to_minutes(t)
            fam = self.class_family.get(cname, "barre_57")
            room = self._find_best_room(room_occ, day_name, fam, start_min, dur, get_class_format(cname))
            if room is None:
                continue
            ts = self.trainer_states.get(trainer)
            prof = self.trainer_profiles.get(trainer, {})
            if ts is None or not prof:
                continue
            prof_loc = prof.get("locations", {}).get(location, {})
            win_s = prof_loc.get("time_window", {}).get("start", "06:00")
            win_e = prof_loc.get("time_window", {}).get("end", "22:00")
            max_d = self._max_per_day(trainer, location, prof_loc.get("max_classes_per_day", 4))
            if not ts.can_add(day_name, t, location, cname, max_d, win_s, win_e):
                continue
            hist = self._get_hist(location, cname, trainer, DOW_REVERSE[day_name], t)
            rooms = LOCATION_ROOMS.get(location, {})
            slot = ScheduleSlot(
                location=location, date=date_str, day_of_week=day_name, time=t,
                class_name=cname, trainer_1=trainer, trainer_2="", cover="",
                room=room, capacity=rooms[room]["capacity"], duration_min=dur,
                predicted_fill_rate=self._evidence_adjusted_fill(hist),
                score=85.0, recommendation="PINNED", is_experimental=False,
                scheduling_reason=f"Pinned block — rule ownership for {trainer}",
                historical_avg_fill=hist.get("avg_fill_rate", 0.0),
                historical_avg_checkin=hist.get("avg_checkin", 0.0),
                historical_session_count=hist.get("session_count", 0),
                historical_late_cancel_rate=hist.get("avg_late_cancel_rate", 0.0),
                historical_no_show_rate=hist.get("avg_no_show_rate", 0.0),
            )
            slots_today.append(slot)
            pinned_times.add(t)
            _register_slot(slot, t)

        # ---- Phase 2: PROTECT combos (score >= 70), subject to per-day format caps ----
        for r in sorted(self.protected.get((location, DOW_REVERSE[day_name]), []),
                        key=lambda x: -x["score"]):
            t = r["time"][:5]
            if t in pinned_times:
                continue
            if slot_is_in_blocked_window(day_name, t):
                continue
            trainer = r["trainer"]
            cname = r["class"]
            # PROTECT_EXACT bypasses per-day format cap (top performer must be placed),
            # but location-level weekly caps still apply.
            wcc = weekly_class_counts or {}
            if "Strength Lab" in cname:
                if location != "Kwality House, Kemps Corner":
                    continue
                if self._weekly_count(wcc, slots_today, cname) >= self._weekly_max(location, cname, 8):
                    continue
            if "HIIT" in cname and self._weekly_count(wcc, slots_today, cname) >= self._weekly_max(location, cname, 3):
                continue
            if "Amped Up" in cname and self._weekly_count(wcc, slots_today, cname) >= self._weekly_max(location, cname, 2):
                continue
            if "Back Body" in cname and self._weekly_count(wcc, slots_today, cname) >= self._weekly_max(location, cname, 3):
                continue
            if self._on_leave(trainer, date_str, location):
                continue
            dur = get_class_duration(cname)
            start_min = slot_time_to_minutes(t)
            fam = self.class_family.get(cname, "barre_57")
            rooms = LOCATION_ROOMS.get(location, {})
            room = self._find_best_room(room_occ, day_name, fam, start_min, dur, get_class_format(cname))
            if room is None:
                continue
            ts = self.trainer_states.get(trainer)
            prof = self.trainer_profiles.get(trainer, {})
            if ts is None or not prof:
                continue
            prof_loc = prof.get("locations", {}).get(location, {})
            win_s = prof_loc.get("time_window", {}).get("start", "06:00")
            win_e = prof_loc.get("time_window", {}).get("end", "22:00")
            max_d = self._max_per_day(trainer, location, prof_loc.get("max_classes_per_day", 4))
            if not ts.can_add(day_name, t, location, cname, max_d, win_s, win_e):
                continue
            if self._would_repeat_consecutive_format(slots_today, t, cname):
                continue
            if would_block_recovery(cname, t, slots_today):
                continue
            if "Recovery" in cname and not is_recovery_last_in_shift(cname, t, slots_today):
                continue
            hist = self._get_hist(location, cname, trainer, DOW_REVERSE[day_name], t)
            slot = ScheduleSlot(
                location=location, date=date_str, day_of_week=day_name, time=t,
                class_name=cname, trainer_1=trainer, trainer_2="", cover="",
                room=room, capacity=rooms[room]["capacity"], duration_min=dur,
                predicted_fill_rate=self._evidence_adjusted_fill(hist),
                score=r["score"], recommendation="PROTECT_EXACT", is_experimental=False,
                scheduling_reason=f"Top performer: {trainer} — {hist.get('session_count',0)} sessions, {hist.get('avg_fill_rate',0):.0%} fill, score {r['score']:.1f}",
                historical_avg_fill=hist.get("avg_fill_rate", 0.0),
                historical_avg_checkin=hist.get("avg_checkin", 0.0),
                historical_session_count=hist.get("session_count", 0),
                historical_late_cancel_rate=hist.get("avg_late_cancel_rate", 0.0),
                historical_no_show_rate=hist.get("avg_no_show_rate", 0.0),
                score_breakdown=r.get("score_breakdown", {}),
            )
            slots_today.append(slot)
            pinned_times.add(t)
            _register_slot(slot, t)
            opt_today += 1

        # ---- Phase 2a: protect class + time even if trainer changes ----
        for r in sorted(self.protected_class_times.get((location, DOW_REVERSE[day_name]), []),
                        key=lambda x: -x["score"]):
            t = r["time"][:5]
            cname = r["class"]
            if t in pinned_times or slot_is_in_blocked_window(day_name, t):
                continue
            if self._would_repeat_consecutive_format(slots_today, t, cname):
                continue
            result = self._fill_slot_class(
                location, day_name, date_str, t, cname, self.class_family.get(cname, "barre_57"),
                used_at_time, shift_trainers, room_occ, slots_today,
                reason_prefix="Protected class/time",
                use_slot_history=True,
                weekly_class_counts=weekly_class_counts,
            )
            if result:
                slots_today.append(result)
                pinned_times.add(t)
                _register_slot(result, t)
                opt_today += 1

        # ---- Phase 2b: Kwality Strength Lab floor ----
        if location == "Kwality House, Kemps Corner" and not is_sunday:
            weekly_floor = self._weekly_min(location, "Studio Strength Lab", 6)
            if weekly_floor > 0:
                day_progress = min(6, DOW_REVERSE[day_name] + 1)
                desired_by_today = max(1, (weekly_floor * day_progress + 5) // 6)
                preferred_times = ["11:00", "11:30", "18:00", "19:15", "09:00", "10:15", "17:45", "18:15"]
                for t in preferred_times:
                    if self._weekly_count(weekly_class_counts, slots_today, "Studio Strength Lab") >= desired_by_today:
                        break
                    if t in pinned_times or slot_is_in_blocked_window(day_name, t):
                        continue
                    result = self._fill_slot_class(
                        location, day_name, date_str, t, "Studio Strength Lab", "strength_lab",
                        used_at_time, shift_trainers, room_occ, slots_today,
                        reason_prefix="Kwality Strength Lab floor",
                        use_slot_history=False,
                        weekly_class_counts=weekly_class_counts,
                    )
                    if result:
                        slots_today.append(result)
                        pinned_times.add(t)
                        _register_slot(result, t)
                        opt_today += 1

        # ---- Phase 2c: Kwality PowerCycle floor in the dedicated studio ----
        if location == "Kwality House, Kemps Corner" and not is_sunday:
            pc_floor = self._weekly_min(location, "Studio PowerCycle", 13)
            day_progress = min(6, DOW_REVERSE[day_name] + 1)
            desired_by_today = max(1, (pc_floor * day_progress + 5) // 6)
            pc_preferred_times = ["19:15", "18:00", "18:15", "17:45", "19:30", "17:30", "09:00", "10:15"]
            for t in pc_preferred_times:
                if self._weekly_count(weekly_class_counts, slots_today, "Studio PowerCycle") >= desired_by_today:
                    break
                if t in pinned_times or slot_is_in_blocked_window(day_name, t):
                    continue
                result = self._fill_slot_class(
                    location, day_name, date_str, t, "Studio PowerCycle", "powercycle",
                    used_at_time, shift_trainers, room_occ, slots_today,
                    reason_prefix="Kwality PowerCycle floor",
                    weekly_class_counts=weekly_class_counts,
                )
                if result:
                    slots_today.append(result)
                    pinned_times.add(t)
                    _register_slot(result, t)
                    opt_today += 1

        # ---- Phase 2d: PowerCycle enforcement for Supreme (SU-001: min 2 PC/weekday; floor 14/wk) ----
        if location == "Supreme HQ, Bandra" and not is_sunday:
            pc_today = sum(1 for s in slots_today if "PowerCycle" in s.class_name)
            pc_target = 3  # bumped from 2 to 3 to satisfy weekly floor of 14
            pc_preferred_times = ["19:00", "19:15", "18:00", "18:15", "17:45", "17:30", "17:00", "16:30", "16:00", "07:30", "08:30"]
            for t in pc_preferred_times:
                if pc_today >= pc_target:
                    break
                if t in pinned_times:
                    continue
                result = self._fill_slot_class(
                    location, day_name, date_str, t, "Studio PowerCycle", "powercycle",
                    used_at_time, shift_trainers, room_occ, slots_today,
                    weekly_class_counts=weekly_class_counts,
                )
                if result:
                    slots_today.append(result)
                    pinned_times.add(t)
                    _register_slot(result, t)
                    pc_today += 1
                    opt_today += 1

        # ---- Phase 3: Determine AM/PM fill targets ----
        locked_count = len(slots_today)
        if locked_count >= target_count:
            return slots_today

        locked_am = sum(1 for s in slots_today if is_am_slot(s.time))
        locked_pm = sum(1 for s in slots_today if not is_am_slot(s.time))

        if is_sunday:
            target_am_fill = target_count - locked_count
            target_pm_fill = 0
        else:
            desired_am = max(1, round(target_count * 0.48))
            desired_pm = target_count - desired_am
            target_am_fill = max(0, desired_am - locked_am)
            target_pm_fill = max(0, desired_pm - locked_pm)

        def _do_fill(t: str, is_prime: bool) -> bool:
            nonlocal exp_today, opt_today
            result = self._fill_slot(location, day_name, date_str, t, used_at_time,
                                     shift_trainers, room_occ, slots_today,
                                     exp_today, opt_today, is_prime=is_prime,
                                     weekly_class_counts=weekly_class_counts,
                                     class_format_count_today=class_format_count_today)
            if result:
                slots_today.append(result)
                _register_slot(result, t)
                if result.is_experimental:
                    exp_today += 1
                else:
                    opt_today += 1
                return True
            return False

        # ---- Phase 4: Prime PM fill ----
        filled_pm = 0
        for t in [s for s in pm_slots if s not in pinned_times and not is_sunday]:
            if filled_pm >= target_pm_fill:
                break
            if _do_fill(t, is_prime=is_prime_slot(t)):
                filled_pm += 1

        # ---- Phase 5: Prime AM fill ----
        filled_am = 0
        for t in [s for s in am_slots if s not in pinned_times]:
            if filled_am >= target_am_fill:
                break
            if slot_is_in_blocked_window(day_name, t):
                continue
            if _do_fill(t, is_prime=is_prime_slot(t)):
                filled_am += 1

        # ---- Phase 6: Top-up if still short ----
        deficit = target_count - len(slots_today)
        if deficit > 0:
            for t in pm_slots + am_slots:
                if deficit <= 0:
                    break
                if slot_is_in_blocked_window(day_name, t):
                    continue
                if _do_fill(t, is_prime=False):
                    deficit -= 1

        return slots_today

    # ------------------------------------------------------------------ #
    #  Single slot fill
    # ------------------------------------------------------------------ #

    def _fill_slot(self, location, day_name, date_str, time_str, used_at_time,
                   shift_trainers, room_occ: RoomOccupancy, slots_today,
                   exp_today, opt_today, is_prime=False,
                   weekly_class_counts: Optional[Dict[str, int]] = None,
                   class_format_count_today: Optional[Dict[str, int]] = None) -> Optional[ScheduleSlot]:
        rooms = LOCATION_ROOMS.get(location, {})
        start_min = slot_time_to_minutes(time_str)
        if slot_is_in_blocked_window(day_name, time_str):
            return None
        dow = DOW_REVERSE[day_name]
        shift = "AM" if is_am_slot(time_str) else "PM"
        already_at_time = used_at_time.get(time_str, set())
        wcc = weekly_class_counts or {}
        fmt_today = class_format_count_today or {}

        pc_kwality_today = sum(1 for s in slots_today if "PowerCycle" in s.class_name)
        pc_kwality_week = wcc.get("Studio PowerCycle", 0) + wcc.get("Studio PowerCycle Express", 0) + pc_kwality_today

        total_today = exp_today + opt_today
        want_experimental = (
            not is_prime and
            total_today > 0 and
            exp_today / total_today < EXPERIMENTAL_RATIO
        )
        def _diversity_adjustment(cname: str) -> float:
            """Return score adjustment based on how many times this format is already today."""
            count = fmt_today.get(cname, 0)
            cap = MAX_FORMAT_PER_DAY.get(cname, DEFAULT_MAX_FORMAT_PER_DAY)
            if count >= cap:
                return -DIVERSITY_PENALTY_OVER_CAP  # blocked — already at cap
            wmin = self._weekly_min(location, cname, WEEKLY_FORMAT_MINIMUMS.get(canonical_class_key(cname), 0))
            weekly_so_far = self._weekly_count(wcc, slots_today, cname)
            weekly_boost = 12.0 if weekly_so_far < wmin else 0.0
            if count == 0:
                return DIVERSITY_BONUS_FIRST + weekly_boost
            if count == 1:
                return DIVERSITY_BONUS_SECOND + weekly_boost
            return 0.0 + weekly_boost

        def _build_candidates(day_filter: bool, allow_drop: bool, seen_tc: Optional[set] = None):
            cands = []
            for r in self.scores_data["class_slot_ranking"]:
                if r["location"] != location:
                    continue
                if day_filter and r["day"] != dow:
                    continue
                r_time = (r.get("time") or "")[:5]
                cname = r["class"]
                fam = self.class_family.get(cname, "")
                if is_excluded_class(cname, fam):
                    continue
                if location == "Kenkere House" and "PowerCycle" in cname:
                    continue
                if location in ("Supreme HQ, Bandra", "Kenkere House") and "Strength Lab" in cname:
                    continue
                trainer = r["trainer"]
                if "PowerCycle" in cname:
                    if not self.trainer_profiles.get(trainer, {}).get("qualifications", {}).get("powercycle", False):
                        continue
                if "Strength Lab" in cname:
                    if not self.trainer_profiles.get(trainer, {}).get("qualifications", {}).get("strength_lab", False):
                        continue
                    if self._weekly_count(wcc, slots_today, cname) >= self._weekly_max(location, cname, 8):
                        continue
                if "PowerCycle" in cname and location == "Kwality House, Kemps Corner":
                    if pc_kwality_week >= self._weekly_max(location, cname, 14):
                        continue
                if "Back Body" in cname and self._weekly_count(wcc, slots_today, cname) >= self._weekly_max(location, cname, 3):
                    continue
                # Hard weekly caps for limited formats
                if "HIIT" in cname and self._weekly_count(wcc, slots_today, cname) >= self._weekly_max(location, cname, 3):
                    continue
                if "Amped Up" in cname and self._weekly_count(wcc, slots_today, cname) >= self._weekly_max(location, cname, 2):
                    continue
                if "Foundations" in cname:
                    if not self.trainer_profiles.get(trainer, {}).get("qualifications", {}).get("foundations", False):
                        continue
                # Recovery slot restriction
                if "Recovery" in cname and not is_recovery_allowed_in_slot(time_str):
                    continue
                if not allow_drop and r.get("recommendation") == "DROP":
                    continue
                if r.get("session_count", 0) < 1:
                    continue
                placement_hist = self._get_hist(location, cname, trainer, dow, time_str)
                has_placement_evidence = placement_hist.get("session_count", 0) > 0
                same_or_near_time = bool(
                    r_time and abs(slot_time_to_minutes(r_time) - start_min) <= 15
                )
                if not has_placement_evidence and not same_or_near_time:
                    continue
                if self._is_inactive(trainer) or self._on_leave(trainer, date_str, location):
                    continue
                if self._loc_excluded(trainer, location):
                    continue
                if trainer in already_at_time:
                    continue
                if seen_tc is not None:
                    key = (trainer, cname)
                    if key in seen_tc:
                        continue
                    seen_tc.add(key)

                div_adj = _diversity_adjustment(cname)
                if div_adj <= -DIVERSITY_PENALTY_OVER_CAP / 2:
                    continue  # format already at daily cap — skip entirely

                # is_exp: truly untested combos only. INCLUDE/PROTECT with 3+ sessions = normal.
                # The diversity bonus (div_adj) is separate from experimental tracking.
                rec = r.get("recommendation", "INCLUDE")
                sc = r.get("session_count", 0)
                is_exp = (rec == "DROP") or (rec == "CONSIDER" and sc < 3)

                if not self._trainer_ok(
                    trainer,
                    location,
                    day_name,
                    time_str,
                    cname,
                    experimental=is_exp and want_experimental,
                ):
                    continue
                dur = get_class_duration(cname)
                class_fam = fam if fam else "barre_57"
                room = self._find_best_room(room_occ, day_name, class_fam, start_min, dur, get_class_format(cname))
                if room is None:
                    continue
                if self._would_repeat_consecutive_format(slots_today, time_str, cname):
                    continue
                # Recovery shift integrity: a non-Recovery placement cannot be later than an existing Recovery in the same shift
                if would_block_recovery(cname, time_str, slots_today):
                    continue
                # Recovery itself must be last-in-shift
                if "Recovery" in cname and not is_recovery_last_in_shift(cname, time_str, slots_today):
                    continue
                state = self.trainer_states.get(trainer)
                if state and state.at_weekly_target():
                    pass

                shift_bonus = 12.0 if trainer in shift_trainers[shift] else (-8.0 if len(shift_trainers[shift]) >= 2 else 0.0)
                time_penalty = 0.0
                if r_time:
                    time_penalty = min(10.0, abs(slot_time_to_minutes(r_time) - start_min) / 30 * 1.5)
                hours_bonus = self._trainer_hours_bonus(trainer, day_name)
                
                # Apply format popularity bonus/penalty
                popularity_bonus = FORMAT_POPULARITY_BONUS.get(cname, 0.0)
                
                rec_for_noise = r.get("recommendation", "INCLUDE")
                ai_delta = self._get_ai_hint_delta(location, cname, trainer, dow)
                effective_score = self._apply_optimization_mode_adjustments(
                    base_score=r["score"],
                    shift_bonus=shift_bonus,
                    diversity_adjustment=div_adj,
                    hours_bonus=hours_bonus,
                    popularity_bonus=popularity_bonus,
                    ai_delta=ai_delta,
                    time_penalty=time_penalty,
                    recommendation=rec_for_noise,
                )

                # Experimental quota: only boost truly untested combos when quota is open.
                if want_experimental and is_exp:
                    effective_score += 15.0

                cands.append((effective_score, r, room, dur, is_exp))
            return cands

        # First pass: same-day, no DROP (strict)
        candidates = _build_candidates(day_filter=True, allow_drop=False)

        if not candidates:
            # Relax to any-day, same location, no DROP
            seen_tc: set = set()
            candidates = _build_candidates(day_filter=False, allow_drop=False, seen_tc=seen_tc)

        if not candidates:
            # Final relaxed pass: any day + allow DROP (last resort before fallback)
            seen_tc2: set = set()
            candidates = _build_candidates(day_filter=False, allow_drop=True, seen_tc=seen_tc2)

        if not candidates:
            return self._fallback_slot(location, day_name, date_str, time_str,
                                       already_at_time, shift_trainers, room_occ, is_prime,
                                       fmt_today=fmt_today, slots_today=slots_today,
                                       weekly_class_counts=weekly_class_counts)

        candidates.sort(key=lambda x: -x[0])
        best_effective, best_r, room, dur, is_exp = candidates[0]
        trainer = best_r["trainer"]
        cname = best_r["class"]
        hist = self._get_hist(location, cname, trainer, dow, time_str)
        score_for_output = round(max(float(best_r["score"]), min(100.0, float(best_effective))), 2)
        score_breakdown = dict(best_r.get("score_breakdown", {}) or {})
        score_breakdown["optimizer_adjusted_score"] = score_for_output

        rec = best_r.get("recommendation", "INCLUDE")
        reason = self._make_reason(cname, trainer, rec, is_exp, hist, score_for_output)
        violations = self._quick_check(location, day_name, time_str, cname, slots_today)

        return ScheduleSlot(
            location=location, date=date_str, day_of_week=day_name, time=time_str,
            class_name=cname, trainer_1=trainer, trainer_2="", cover="",
            room=room, capacity=rooms.get(room, {}).get("capacity", 15),
            duration_min=dur,
            predicted_fill_rate=self._evidence_adjusted_fill(hist),
            score=score_for_output, recommendation=rec,
            is_experimental=is_exp,
            scheduling_reason=reason,
            historical_avg_fill=hist.get("avg_fill_rate", 0.0),
            historical_avg_checkin=hist.get("avg_checkin", 0.0),
            historical_session_count=hist.get("session_count", 0),
                historical_late_cancel_rate=hist.get("avg_late_cancel_rate", 0.0),
                historical_no_show_rate=hist.get("avg_no_show_rate", 0.0),
                score_breakdown=score_breakdown,
            constraint_violations=violations,
        )

    def _fallback_slot(self, location, day_name, date_str, time_str,
                       already_at_time, shift_trainers, room_occ: RoomOccupancy,
                       is_prime, fmt_today: Optional[Dict[str, int]] = None,
                       slots_today: Optional[List["ScheduleSlot"]] = None,
                       weekly_class_counts: Optional[Dict[str, int]] = None) -> Optional[ScheduleSlot]:
        rooms = LOCATION_ROOMS.get(location, {})
        start_min = slot_time_to_minutes(time_str)
        shift = "AM" if is_am_slot(time_str) else "PM"
        fmt = fmt_today or {}
        st_today = slots_today or []
        wcc = weekly_class_counts or {}
        if slot_is_in_blocked_window(day_name, time_str):
            return None

        # Order fallback formats by how many times they've already appeared today
        # (prefer formats that haven't been used yet)
        fallback_candidates = [
            ("Studio FIT", "barre_57", 57),
            ("Studio Back Body Blaze", "barre_57", 57),
            ("Studio Cardio Barre", "barre_57", 57),
            ("Studio Foundations", "barre_57", 57),
            ("Studio Mat 57", "barre_57", 57),
            ("Studio Barre 57", "barre_57", 57),
        ]
        fallback_candidates.sort(key=lambda x: fmt.get(x[0], 0))

        def priority(name, state):
            in_shift = name in shift_trainers[shift]
            return (0 if in_shift else 1, state.tier, name)

        for cname, fam, dur in fallback_candidates:
            cap = MAX_FORMAT_PER_DAY.get(cname, DEFAULT_MAX_FORMAT_PER_DAY)
            if fmt.get(cname, 0) >= cap:
                continue
            # Weekly cap checks for specialized formats
            if "Strength Lab" in cname and self._weekly_count(wcc, st_today, cname) >= self._weekly_max(location, cname, 8):
                continue
            if "HIIT" in cname and self._weekly_count(wcc, st_today, cname) >= self._weekly_max(location, cname, 3):
                continue
            if "Amped Up" in cname and self._weekly_count(wcc, st_today, cname) >= self._weekly_max(location, cname, 2):
                continue
            if "Back Body" in cname and self._weekly_count(wcc, st_today, cname) >= self._weekly_max(location, cname, 3):
                continue
            if location == "Kenkere House" and "PowerCycle" in cname:
                continue
            if is_excluded_class(cname, fam):
                continue
            room = self._find_best_room(room_occ, day_name, fam, start_min, dur, get_class_format(cname))
            if room is None:
                continue
            if self._would_repeat_consecutive_format(st_today, time_str, cname):
                continue
            if would_block_recovery(cname, time_str, st_today):
                continue

            for name, state in sorted(self.trainer_states.items(), key=lambda x: priority(x[0], x[1])):
                if self._is_inactive(name) or self._on_leave(name, date_str, location):
                    continue
                if self._loc_excluded(name, location):
                    continue
                if name in already_at_time:
                    continue
                # Don't skip at target — deprioritize by sorting (handled via priority fn)
                if not self._trainer_ok(name, location, day_name, time_str, cname):
                    continue
                # Foundations needs certified trainer
                if "Foundations" in cname:
                    if not self.trainer_profiles.get(name, {}).get("qualifications", {}).get("foundations", False):
                        continue
                hist = self._get_hist(location, cname, name, DOW_REVERSE[day_name], time_str)
                fallback_score = 55.0 if hist.get("session_count", 0) > 0 else 45.0
                return ScheduleSlot(
                    location=location, date=date_str, day_of_week=day_name, time=time_str,
                    class_name=cname, trainer_1=name, trainer_2="", cover="",
                    room=room, capacity=rooms.get(room, {}).get("capacity", 15),
                    duration_min=dur,
                    predicted_fill_rate=self._evidence_adjusted_fill(hist),
                    score=fallback_score, recommendation="INCLUDE", is_experimental=False,
                    scheduling_reason=f"Coverage: best available trainer-class combo for this slot",
                    historical_avg_fill=hist.get("avg_fill_rate", 0.0),
                    historical_avg_checkin=hist.get("avg_checkin", 0.0),
                    historical_session_count=hist.get("session_count", 0),
                historical_late_cancel_rate=hist.get("avg_late_cancel_rate", 0.0),
                historical_no_show_rate=hist.get("avg_no_show_rate", 0.0),
                )
        return None

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    def _fill_slot_class(self, location, day_name, date_str, time_str, class_name, class_fam,
                         used_at_time, shift_trainers, room_occ: RoomOccupancy,
                         slots_today, reason_prefix: str = "Enforced class slot",
                         use_slot_history: bool = False,
                         weekly_class_counts: Optional[Dict[str, int]] = None) -> Optional["ScheduleSlot"]:
        # Hard weekly caps
        wcc = weekly_class_counts or {}
        if "Strength Lab" in class_name:
            if location != "Kwality House, Kemps Corner":
                return None
            if self._weekly_count(wcc, slots_today, class_name) >= self._weekly_max(location, class_name, 8):
                return None
        if "HIIT" in class_name and self._weekly_count(wcc, slots_today, class_name) >= self._weekly_max(location, class_name, 3):
            return None
        if "Amped Up" in class_name and self._weekly_count(wcc, slots_today, class_name) >= self._weekly_max(location, class_name, 2):
            return None
        if "Back Body" in class_name and self._weekly_count(wcc, slots_today, class_name) >= self._weekly_max(location, class_name, 3):
            return None
        """Force-schedule a specific class at a specific time, picking the best qualified trainer.
        
        Args:
            use_slot_history: If True, use aggregated slot-level historical data (all trainers).
                             If False, use trainer-specific historical data.
        """
        rooms_def = LOCATION_ROOMS.get(location, {})
        start_min = slot_time_to_minutes(time_str)
        if slot_is_in_blocked_window(day_name, time_str):
            return None
        dur = get_class_duration(class_name)
        shift = "AM" if is_am_slot(time_str) else "PM"

        # Collect ranked candidates from scored data
        candidates = []
        dow = DOW_REVERSE[day_name]
        for r in self.scores_data["class_slot_ranking"]:
            if r["location"] != location or r["class"] != class_name:
                continue
            r_time = (r.get("time") or "")[:5]
            if r["day"] != dow:
                continue
            if use_slot_history and r_time != time_str:
                continue
            trainer = r["trainer"]
            tp = self.trainer_profiles.get(trainer, {})
            if not tp.get("qualifications", {}).get("powercycle" if "Cycle" in class_name else "strength_lab" if "Strength" in class_name else "all_barre", False):
                continue
            if self._is_inactive(trainer) or self._on_leave(trainer, date_str, location):
                continue
            already = used_at_time.get(time_str, set())
            if trainer in already:
                continue
            if not self._trainer_ok(trainer, location, day_name, time_str, class_name):
                continue
            room = self._find_best_room(room_occ, day_name, class_fam, start_min, dur, get_class_format(class_name))
            if room is None:
                continue
            if self._would_repeat_consecutive_format(slots_today, time_str, class_name):
                continue
            if would_block_recovery(class_name, time_str, slots_today):
                continue
            if "Recovery" in class_name and not is_recovery_last_in_shift(class_name, time_str, slots_today):
                continue
            shift_bonus = 12.0 if trainer in shift_trainers[shift] else 0.0
            time_penalty = 0.0
            if r_time:
                time_penalty = min(10.0, abs(slot_time_to_minutes(r_time) - start_min) / 30 * 1.5)
            ai_delta = self._get_ai_hint_delta(location, class_name, trainer, dow)
            candidates.append((r["score"] + shift_bonus + self._trainer_hours_bonus(trainer, day_name) + ai_delta - time_penalty, r, room))

        if not candidates:
            return None

        candidates.sort(key=lambda x: -x[0])
        best_effective, best_r, room = candidates[0]
        trainer = best_r["trainer"]
        score_for_output = round(max(float(best_r["score"]), min(100.0, float(best_effective))), 2)
        score_breakdown = dict(best_r.get("score_breakdown", {}) or {})
        score_breakdown["optimizer_adjusted_score"] = score_for_output
        
        # Choose historical data source
        if use_slot_history:
            hist = self._get_hist_slot(location, class_name, dow, time_str)
            reason_detail = f"Protected slot — alternate trainer selected. Slot history: {hist.get('session_count',0)} sessions, {hist.get('avg_fill_rate',0):.0%} fill, {hist.get('avg_checkin',0):.1f} avg check-ins"
        else:
            hist = self._get_hist(location, class_name, trainer, dow, time_str)
            reason_detail = f"{reason_prefix}: {class_name}. Score {best_r['score']:.1f}; trainer chosen for fill-rate potential, trainer-hour balance, and class-mix logic."

        return ScheduleSlot(
            location=location, date=date_str, day_of_week=day_name, time=time_str,
            class_name=class_name, trainer_1=trainer, trainer_2="", cover="",
            room=room, capacity=rooms_def.get(room, {}).get("capacity", 14),
            duration_min=dur,
            predicted_fill_rate=self._evidence_adjusted_fill(hist),
            score=score_for_output, recommendation=best_r.get("recommendation", "INCLUDE"),
            is_experimental=False,
            scheduling_reason=reason_detail,
            historical_avg_fill=hist.get("avg_fill_rate", 0.0),
            historical_avg_checkin=hist.get("avg_checkin", 0.0),
            historical_session_count=hist.get("session_count", 0),
                historical_late_cancel_rate=hist.get("avg_late_cancel_rate", 0.0),
                historical_no_show_rate=hist.get("avg_no_show_rate", 0.0),
                score_breakdown=score_breakdown,
        )

    def _find_best_room(self, room_occ: RoomOccupancy, day: str, class_fam: str,
                        start_min: int, duration: int, class_format: str) -> Optional[str]:
        """Find a room that is available and (preferably) doesn't repeat the same format.
        Tries non-repeating rooms first, then falls back to repeating-format rooms."""
        all_rooms = room_occ.rooms  # Dict[room_id, config]

        # Determine candidate rooms in priority order
        if class_fam == "strength_lab":
            candidates = [r for r in all_rooms if r == "strength_lab"]
        elif class_fam == "powercycle":
            candidates = [r for r in all_rooms if r == "powercycle"]
        else:
            candidates = [r for r in all_rooms if r not in ("strength_lab", "powercycle")]
            candidates = sorted(candidates, key=lambda r: {"studio_a": 0, "studio_b": 1}.get(r, 2))

        # First pass: prefer room where format won't repeat
        repeat_fallback = None
        for room_id in candidates:
            if not room_occ.is_available(day, room_id, start_min, duration):
                continue
            last = room_occ.last_class_in_room(day, room_id)
            if last and get_class_format(last) == class_format:
                repeat_fallback = repeat_fallback or room_id
                continue  # would repeat format — try next room
            return room_id

        return repeat_fallback

    def _trainer_ok(self, trainer, location, day_name, time_str, class_name, experimental: bool = False) -> bool:
        profile = self.trainer_profiles.get(trainer)
        if not profile:
            return False
        loc_data = profile.get("locations", {}).get(location)
        if not loc_data:
            if not experimental:
                return False
            loc_candidates = list(profile.get("locations", {}).values())
            if not loc_candidates:
                return False
            loc_data = max(loc_candidates, key=lambda item: item.get("session_count", 0))
        avail_days = self._available_days(trainer, location, loc_data.get("available_days", []))
        if day_name not in avail_days and not experimental:
            return False
        tw = loc_data.get("time_window", {})
        win_s, win_e = self._time_window(trainer, location,
                                         tw.get("start", "06:00"), tw.get("end", "22:00"))
        max_d = self._max_per_day(trainer, location, loc_data.get("max_classes_per_day", 4))
        state = self.trainer_states.get(trainer)
        if state is None:
            return False
        if day_name not in state.worked_days() and state.worked_days_count() >= 6:
            return False
        if not state.can_add(day_name, time_str, location, class_name, max_d, win_s, win_e):
            return False
        max_w = self._max_per_week(trainer, location)
        if max_w is not None:
            loc_total = sum(1 for d in state._schedule.values()
                            for _, loc, _ in d if loc == location)
            if loc_total >= max_w:
                return False
        return True

    def _trainer_hours_bonus(self, trainer: str, day_name: str) -> float:
        state = self.trainer_states.get(trainer)
        if not state:
            return 0.0
        bonus = 0.0
        if state.tier == 1:
            remaining = max(0, TIER1_WEEKLY_TARGET_MIN - state.weekly_minutes)
            bonus += min(18.0, remaining / 60 * 1.25)
            if state.weekly_minutes > TIER1_WEEKLY_TARGET_MIN:
                bonus -= min(10.0, (state.weekly_minutes - TIER1_WEEKLY_TARGET_MIN) / 60 * 1.5)
        if day_name not in state.worked_days():
            if state.worked_days_count() >= 5:
                bonus -= 8.0
            elif state.worked_days_count() <= 3:
                bonus += 3.0
        return bonus

    def _apply_optimization_mode_adjustments(
        self,
        base_score: float,
        shift_bonus: float,
        diversity_adjustment: float,
        hours_bonus: float,
        popularity_bonus: float,
        ai_delta: float,
        time_penalty: float,
        recommendation: str,
    ) -> float:
        if self._optimization_mode == "trainer_hours":
            hours_bonus *= 2.6
            shift_bonus *= 1.2
            diversity_adjustment *= 0.75
        elif self._optimization_mode == "class_variety":
            diversity_adjustment *= 2.4
            popularity_bonus *= 0.5
            hours_bonus *= 0.65
        else:
            hours_bonus *= 0.65
            diversity_adjustment *= 0.8

        return (
            base_score
            + shift_bonus
            + diversity_adjustment
            + hours_bonus
            + popularity_bonus
            + ai_delta
            - time_penalty
            + self._score_noise(recommendation)
        )

    def _would_repeat_consecutive_format(self, slots_today: List[ScheduleSlot], time_str: str, class_name: str) -> bool:
        if not slots_today:
            return False
        current_min = slot_time_to_minutes(time_str)
        current_format = get_class_format(class_name)
        # 1) Direct nearest-neighbour check
        earlier = [s for s in slots_today if slot_time_to_minutes(s.time) < current_min]
        later = [s for s in slots_today if slot_time_to_minutes(s.time) > current_min]
        nearest_earlier = max(earlier, key=lambda s: slot_time_to_minutes(s.time), default=None)
        nearest_later = min(later, key=lambda s: slot_time_to_minutes(s.time), default=None)
        neighbours = [s for s in (nearest_earlier, nearest_later) if s is not None]
        if any(get_class_format(s.class_name) == current_format for s in neighbours):
            return True
        # 2) Anything within 60 minutes (either side) of same family
        for s in slots_today:
            if abs(slot_time_to_minutes(s.time) - current_min) <= 60:
                if get_class_format(s.class_name) == current_format:
                    return True
        return False

    def _get_hist(self, location, class_name, trainer, day_int, time_str) -> dict:
        t = time_str[:5] if time_str else ""
        key = (location, class_name, trainer, day_int, t)
        if key in self.hist_lookup:
            return self.hist_lookup[key]
        # Try nearby times (+-15 min)
        t_min = slot_time_to_minutes(t) if t else 0
        for (loc, cls, tr, d, kt), v in self.hist_lookup.items():
            if loc == location and cls == class_name and tr == trainer and d == day_int:
                if kt and abs(slot_time_to_minutes(kt) - t_min) <= 15:
                    return v
        return self._get_hist_slot(location, class_name, day_int, time_str)

    def _get_hist_slot(self, location, class_name, day_int, time_str) -> dict:
        """Get aggregated historical data for a class+location+day+time slot, regardless of trainer."""
        t = time_str[:5] if time_str else ""
        # Aggregate all trainer data for this slot
        matching = []
        for (loc, cls, tr, d, kt), v in self.hist_lookup.items():
            if loc == location and cls == class_name and d == day_int and kt == t:
                matching.append(v)
        
        if not matching:
            t_min = slot_time_to_minutes(t) if t else 0
            for (loc, cls, tr, d, kt), v in self.hist_lookup.items():
                if loc == location and cls == class_name and d == day_int and kt:
                    if abs(slot_time_to_minutes(kt) - t_min) <= 15:
                        matching.append(v)
        
        if not matching:
            return {}
        
        # Aggregate metrics
        total_sessions = sum(m.get("session_count", 0) for m in matching)
        if total_sessions == 0:
            return {}
        
        # Weighted average by session count
        avg_fill = sum(m.get("avg_fill_rate", 0) * m.get("session_count", 0) for m in matching) / total_sessions
        avg_checkin = sum(m.get("avg_checkin", 0) * m.get("session_count", 0) for m in matching) / total_sessions
        avg_late_cancel = sum(m.get("avg_late_cancel_rate", 0) * m.get("session_count", 0) for m in matching) / total_sessions
        avg_no_show = sum(m.get("avg_no_show_rate", 0) * m.get("session_count", 0) for m in matching) / total_sessions
        
        return {
            "session_count": total_sessions,
            "avg_fill_rate": avg_fill,
            "avg_checkin": avg_checkin,
            "avg_late_cancel_rate": avg_late_cancel,
            "avg_no_show_rate": avg_no_show,
        }

    def _make_reason(self, cname, trainer, rec, is_exp, hist, score) -> str:
        if is_exp:
            sc = hist.get("session_count", 0)
            if sc == 0:
                return f"Experimental: no historical data for {cname} with {trainer} — included for class variety"
            return f"Experimental: {sc} sessions, fill {hist.get('avg_fill_rate',0):.0%} — included to diversify schedule"
        base = f"Score {score:.1f}/100 ({rec})"
        if hist:
            base += f" — {hist.get('session_count',0)} sessions, avg fill {hist.get('avg_fill_rate',0):.0%}, avg check-in {hist.get('avg_checkin',0):.1f}"
        return base

    def _quick_check(self, location, day_name, time_str, cname, slots_today) -> List[str]:
        v = build_constraint_violations(location, day_name, time_str, cname, slots_today)
        if self._would_repeat_consecutive_format(slots_today, time_str, cname):
            v.append("UNIV-023: Consecutive class format")
        return v

    def _get_pinned_slots(self, location, day_name) -> List[dict]:
        p = []
        if location == "Kwality House, Kemps Corner":
            if day_name == "Thursday":
                p += [
                    {"time": "09:15", "trainer": "Rohan Dahima", "class": "Studio Barre 57"},
                    {"time": "10:15", "trainer": "Rohan Dahima", "class": "Studio Back Body Blaze"},
                ]
            if day_name == "Saturday":
                p += [
                    {"time": "10:15", "trainer": "Pranjali Jain", "class": "Studio Barre 57"},
                    {"time": "11:30", "trainer": "Pranjali Jain", "class": "Studio Barre 57"},
                    {"time": "12:30", "trainer": "Pranjali Jain", "class": "Studio Mat 57"},
                ]
            if day_name in ("Monday", "Wednesday"):
                p += [{"time": "18:00", "trainer": "Atulan Purohit", "class": "Studio Strength Lab"}]
        elif location == "Supreme HQ, Bandra":
            if day_name == "Thursday":
                p += [
                    {"time": "08:00", "trainer": "Anisha Shah", "class": "Studio Barre 57"},
                    {"time": "09:00", "trainer": "Anisha Shah", "class": "Studio Back Body Blaze"},
                    {"time": "10:00", "trainer": "Anisha Shah", "class": "Studio Barre 57"},
                    {"time": "11:00", "trainer": "Anisha Shah", "class": "Studio Mat 57"},
                ]
            if day_name in ("Tuesday", "Wednesday"):
                p += [
                    {"time": "07:30", "trainer": "Vivaran Dhasmana", "class": "Studio Barre 57"},
                    {"time": "09:00", "trainer": "Vivaran Dhasmana", "class": "Studio Back Body Blaze"},
                    {"time": "10:00", "trainer": "Vivaran Dhasmana", "class": "Studio Barre 57"},
                    {"time": "11:00", "trainer": "Vivaran Dhasmana", "class": "Studio Mat 57"},
                ]
            if day_name == "Sunday":
                p += [
                    {"time": "10:15", "trainer": "Karan Bhatia", "class": "Studio Barre 57"},
                    {"time": "11:30", "trainer": "Karan Bhatia", "class": "Studio Mat 57"},
                ]
            if day_name in ("Friday", "Saturday"):
                p += [
                    {"time": "09:00", "trainer": "Atulan Purohit", "class": "Studio Barre 57"},
                    {"time": "10:00", "trainer": "Atulan Purohit", "class": "Studio Back Body Blaze"},
                    {"time": "11:00", "trainer": "Atulan Purohit", "class": "Studio Barre 57"},
                ]
        elif location == "Kenkere House":
            if day_name in ("Monday", "Tuesday", "Thursday"):
                p += [
                    {"time": "07:15", "trainer": "Pushyank Nahar", "class": "Studio Barre 57"},
                    {"time": "09:00", "trainer": "Pushyank Nahar", "class": "Studio Back Body Blaze"},
                    {"time": "11:00", "trainer": "Pushyank Nahar", "class": "Studio Barre 57"},
                ]
            if day_name == "Friday":
                p += [
                    {"time": "07:15", "trainer": "Shruti Kulkarni", "class": "Studio Barre 57"},
                    {"time": "09:00", "trainer": "Shruti Kulkarni", "class": "Studio Back Body Blaze"},
                ]
            if day_name == "Sunday":
                p += [
                    {"time": "10:00", "trainer": "Shruti Kulkarni", "class": "Studio Mat 57"},
                    {"time": "11:00", "trainer": "Shruti Kulkarni", "class": "Studio Barre 57"},
                ]
            if day_name == "Saturday":
                p += [
                    {"time": "09:00", "trainer": "Kajol Kanchan", "class": "Studio Barre 57"},
                    {"time": "10:00", "trainer": "Kajol Kanchan", "class": "Studio Back Body Blaze"},
                    {"time": "11:00", "trainer": "Kajol Kanchan", "class": "Studio Barre 57"},
                ]
        return p
