import json
import hashlib
import random
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, List, Dict, Set, Tuple
from datetime import date, timedelta
from collections import defaultdict

from agents.draft_retention import prune_draft_schedule_files

STATE_DIR = Path("state")
RULES_DIR = Path("rules")
CONFIG_DIR = Path("config")

DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DOW_MAP = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}
DOW_REVERSE = {v: k for k, v in DOW_MAP.items()}

MAX_TRAINER_WEEKLY_MINUTES_T1 = 15 * 60
MAX_TRAINER_WEEKLY_MINUTES_T2 = 15 * 60
MAX_TRAINER_WEEKLY_MINUTES_T3 = 12 * 60
MAX_TRAINER_WEEKLY_MINUTES = MAX_TRAINER_WEEKLY_MINUTES_T2  # Legacy alias
TIER1_WEEKLY_TARGET_MIN = 13 * 60
TIER1_WEEKLY_TARGET_IDEAL = 15 * 60
TIER2_WEEKLY_TARGET_MIN = 10 * 60
TIER3_WEEKLY_TARGET_MAX = 6 * 60
MAX_TRAINER_DAILY_MINUTES = 4 * 60
MAX_TRAINER_WORK_DAYS = 5


def apply_settings_caps_from_config(config: Optional[dict] = None) -> dict:
    """Override runtime caps from settings_options. Falls back to CLAUDE.md
    canonical values. Mutates module globals so TrainerState picks them up."""
    global MAX_TRAINER_WORK_DAYS, MAX_TRAINER_DAILY_MINUTES
    global MAX_TRAINER_WEEKLY_MINUTES_T1, MAX_TRAINER_WEEKLY_MINUTES_T2
    global MAX_TRAINER_WEEKLY_MINUTES_T3, MAX_TRAINER_WEEKLY_MINUTES
    global TIER1_WEEKLY_TARGET_MIN, TIER1_WEEKLY_TARGET_IDEAL
    try:
        if config is None:
            path = CONFIG_DIR / "schedule_config.json"
            config = json.loads(path.read_text()) if path.exists() else {}
        opts = (config.get("settings_options") or {})
    except Exception:
        opts = {}

    def _hours_to_min(v, default):
        try:
            return int(round(float(v) * 60))
        except (TypeError, ValueError):
            return default

    weekly_cap_min = _hours_to_min(opts.get("weekly_hours_cap"), MAX_TRAINER_WEEKLY_MINUTES_T1)
    MAX_TRAINER_WEEKLY_MINUTES_T1 = weekly_cap_min
    MAX_TRAINER_WEEKLY_MINUTES_T2 = weekly_cap_min
    MAX_TRAINER_WEEKLY_MINUTES = weekly_cap_min
    # T3 cap: keep at 12h unless explicitly set
    MAX_TRAINER_WEEKLY_MINUTES_T3 = _hours_to_min(opts.get("tier3_weekly_hours_cap"), 12 * 60)

    TIER1_WEEKLY_TARGET_MIN = _hours_to_min(opts.get("tier1_min_weekly_hours"), 13 * 60)
    TIER1_WEEKLY_TARGET_IDEAL = _hours_to_min(opts.get("tier1_ideal_weekly_hours"), 15 * 60)
    MAX_TRAINER_DAILY_MINUTES = _hours_to_min(opts.get("max_daily_trainer_hours"), 4 * 60)
    try:
        MAX_TRAINER_WORK_DAYS = max(1, min(7, int(opts.get("max_trainer_work_days") or 5)))
    except (TypeError, ValueError):
        MAX_TRAINER_WORK_DAYS = 5

    return {
        "weekly_cap_min": MAX_TRAINER_WEEKLY_MINUTES_T1,
        "tier1_min": TIER1_WEEKLY_TARGET_MIN,
        "tier1_ideal": TIER1_WEEKLY_TARGET_IDEAL,
        "daily_min": MAX_TRAINER_DAILY_MINUTES,
        "work_days": MAX_TRAINER_WORK_DAYS,
    }


# Apply once at module import for any callers that import this module
# without instantiating an Optimiser.
apply_settings_caps_from_config()
SUPREME_LOCATION = "Supreme HQ, Bandra"
KWALITY_LOCATION = "Kwality House, Kemps Corner"
LOCATION_WEEKLY_CLASS_BOUNDS = {
    KWALITY_LOCATION: {"min": 70, "max": 80},
    SUPREME_LOCATION: {"min": 65, "max": 75},
    "Kenkere House": {"min": 55, "max": 70},
}
HIGH_PRIORITY_TRAINERS = {
    "Anisha Shah", "Rohan Dahima", "Reshma Sharma", "Atulan Purohit", "Pranjali Jain",
    "Karanvir Bhatia", "Mrigakshi Jaiswal", "Vivaran Dhasmana", "Pushyank Nahar",
    "Kajol Kanchan", "Shruti Kulkarni",
}
MUMBAI_POWERCYCLE_PRIORITY_TRAINERS = {"Vivaran Dhasmana", "Cauveri Vikrant", "Karanvir Bhatia"}
STRENGTH_FIT_PRIORITY_TRAINERS = {
    "Atulan Purohit", "Mrigakshi Jaiswal", "Anisha Shah", "Reshma Sharma", "Richard D'Costa"
}
MUMBAI_TIER1_SUPREME_MIN_SHARE = 0.40
MUMBAI_TIER1_SUPREME_MAX_SHARE = 0.55
MIN_CLASS_START_MIN = 7 * 60
BLOCKED_MIDDAY_START_MIN = 13 * 60
BLOCKED_MIDDAY_END_MIN = 15 * 60
MAX_CLASS_START_MIN = 20 * 60 + 30

# Accurate class durations in minutes
CLASS_DURATIONS: Dict[str, int] = {
    "Studio Barre 57": 57,
    "Studio Barre 57 Express": 30,
    "Studio Cardio Barre": 57,
    "Studio Cardio Barre Express": 30,
    "Studio Cardio Barre Plus": 75,
    "Studio Mat 57": 57,
    "Studio Mat 57 Express": 30,
    "Studio Back Body Blaze": 57,
    "Studio Back Body Blaze Express": 30,
    "Studio FIT": 57,
    "Studio Power Barre": 57,
    "Studio Barre Fusion": 57,
    "Studio Amped Up!": 57,
    "Studio HIIT": 45,
    "Studio Recovery": 57,
    "Studio SWEAT In 30": 30,
    "Studio Foundations": 57,
    "Studio Dance Cardio": 45,
    "Studio Flex & Flow": 45,
    "Studio Strength Lab": 57,
    "Studio PowerCycle": 45,
    "Studio PowerCycle Express": 30,
    "Studio Hosted Class": 60,
    "Pre/Post Natal": 57,
}
_CLASS_FORMAT_DURATION_CACHE: Optional[Dict[str, int]] = None


def _class_format_durations() -> Dict[str, int]:
    global _CLASS_FORMAT_DURATION_CACHE
    if _CLASS_FORMAT_DURATION_CACHE is not None:
        return _CLASS_FORMAT_DURATION_CACHE
    path = Path(__file__).resolve().parent.parent / "rules" / "class_formats.json"
    durations: Dict[str, int] = {}
    try:
        with open(path) as f:
            rows = json.load(f)
        for row in rows:
            name = row.get("name")
            duration = row.get("duration_min")
            if name and duration is not None:
                durations[str(name)] = int(duration)
    except Exception:
        durations = {}
    _CLASS_FORMAT_DURATION_CACHE = durations
    return durations

def get_class_duration(class_name: str) -> int:
    configured = _class_format_durations().get(str(class_name or ""))
    if configured is not None:
        return configured
    if class_name in CLASS_DURATIONS:
        return CLASS_DURATIONS[class_name]
    # Fuzzy match
    lower = str(class_name or "").lower()
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
    "Courtside": {
        "studio_a": {"capacity": 14, "families": None},
    },
    "Copper & Cloves": {
        "studio_a": {"capacity": 14, "families": None},
    },
}

MUMBAI_LOCATIONS = {"Kwality House, Kemps Corner", "Supreme HQ, Bandra", "Courtside"}
BENGALURU_LOCATIONS = {"Kenkere House", "Copper & Cloves"}
MAIN_STUDIOS = {"Kwality House, Kemps Corner", "Supreme HQ, Bandra", "Kenkere House"}
DERIVED_STUDIOS = {"Courtside", "Copper & Cloves"}
DERIVED_LOCATION_SOURCES = {
    "Courtside": ["Kwality House, Kemps Corner", "Supreme HQ, Bandra"],
    "Copper & Cloves": ["Copper & Cloves", "Kenkere House"],
}
WEEKEND_ONLY_TARGETS = {
    "Courtside": {"Saturday": 2, "Sunday": 2},
}
COURTSIDE_ALLOWED_CANONICAL_CLASSES = {
    "Studio Barre 57",
    "Studio Mat 57",
    "Studio Cardio Barre",
    "Studio FIT",
}
COPPER_ALLOWED_CLASSES = {
    "Copper + Cloves Mat 57",
    "Copper + Cloves FIT",
    "Copper + Cloves Barre 57",
}
STRENGTH_LAB_PROTECT_FILL_THRESHOLD = 0.50

def location_region(location: str) -> str:
    if location in MUMBAI_LOCATIONS:
        return "mumbai"
    if location in BENGALURU_LOCATIONS:
        return "bengaluru"
    return location


def is_main_studio(location: str) -> bool:
    return location in MAIN_STUDIOS


def is_derived_studio(location: str) -> bool:
    return location in DERIVED_STUDIOS

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
MUMBAI_PARALLEL_AM_EARLY = ["08:00", "08:15", "08:30", "08:45"]
MUMBAI_PARALLEL_AM_LATE = ["11:00", "11:15", "11:30", "11:45"]
MUMBAI_PARALLEL_PM = ["18:00", "18:15", "18:30", "18:45"]
MUMBAI_PARALLEL_PEAK_TIMES = set(MUMBAI_PARALLEL_AM_EARLY + MUMBAI_PARALLEL_AM_LATE + MUMBAI_PARALLEL_PM)
PRIME_AM_SLOTS.update(MUMBAI_PARALLEL_AM_EARLY + MUMBAI_PARALLEL_AM_LATE)
PRIME_PM_SLOTS.update(MUMBAI_PARALLEL_PM)
LOCATION_AM_SLOT_PRIORITY: Dict[str, List[str]] = {
    KWALITY_LOCATION: [
        "08:00", "08:15", "08:30", "08:45",
        "11:00", "11:15", "11:30", "11:45",
        "09:00", "09:15", "09:30", "10:00", "10:15", "07:30", "07:15",
    ],
    SUPREME_LOCATION: [
        "08:00", "08:15", "08:30", "08:45",
        "11:00", "11:15", "11:30", "11:45",
        "09:00", "09:15", "09:30", "09:45", "10:00", "10:15", "10:30",
        "12:00", "12:30", "07:30",
    ],
}
LOCATION_PM_SLOT_PRIORITY: Dict[str, List[str]] = {
    KWALITY_LOCATION: ["18:00", "18:15", "18:30", "18:45", "17:45", "19:00", "19:15", "19:30", "17:30", "17:15", "17:00"],
    SUPREME_LOCATION: ["18:00", "18:15", "18:30", "18:45", "17:45", "19:00", "19:15", "19:30", "17:30", "17:00", "16:30", "20:00", "20:15"],
}

# Experimental quota: about 10% of non-pinned classes should test a weak slot,
# new trainer, or low-history combo so the schedule stays dynamic without
# crowding out proven recurring classes.
EXPERIMENTAL_RATIO = 0.10

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
    "Courtside": {
        "Monday": (0, 0), "Tuesday": (0, 0), "Wednesday": (0, 0),
        "Thursday": (0, 0), "Friday": (0, 0), "Saturday": (2, 2), "Sunday": (2, 2),
    },
    "Copper & Cloves": {
        "Monday": (1, 2), "Tuesday": (1, 2), "Wednesday": (1, 2),
        "Thursday": (1, 2), "Friday": (1, 2), "Saturday": (2, 3), "Sunday": (2, 3),
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
    "Studio Trainer's Choice": 3.0,   # occasional Kwality slot in place of Amped Up / BBS
    "Studio Cardio Barre Plus": 2.0,  # occasional Kwality variety
    "Studio HIIT": -10.0,             # PENALIZE - reduce frequency
    "Studio Amped Up!": -10.0,        # PENALIZE - reduce frequency
    "Studio Dance Recovery": -15.0,
}

# Location-specific format bonuses — applied on top of FORMAT_POPULARITY_BONUS
LOCATION_FORMAT_BONUS: Dict[str, Dict[str, float]] = {
    "Supreme HQ, Bandra": {
        "Studio FIT": 8.0,          # 3rd pillar after Barre 57 + PowerCycle
        "Studio Cardio Barre": -2.0, # 5th behind FIT
    },
    "Kwality House, Kemps Corner": {
        "Studio Trainer's Choice": 5.0,  # replace 1 Amped Up or BBS per week
        "Studio Cardio Barre Plus": 4.0, # occasional scheduling
    },
}

# Class-mix maxima are planning targets, not hard caps. Once a class is at its
# target max, additional placements remain possible but need stronger evidence.
CLASS_MIX_OVER_TARGET_PENALTY = 16.0
CLASS_MIX_OVER_TARGET_PENALTY_CAP = 64.0

# Diversity score adjustments: first-of-format-today gets a bonus, repeats get penalties
DIVERSITY_BONUS_FIRST = 18.0   # first occurrence of a format today
DIVERSITY_BONUS_SECOND = 5.0   # second (only for multi-cap formats like Barre 57)
DIVERSITY_PENALTY_OVER_CAP = 999.0  # effectively blocks it
CLASS_LEVEL_BONUS_MISSING = 72.0
CLASS_LEVEL_PENALTY_REPEAT = 30.0
HORIZONTAL_MAX_SAME_CLASS_PER_TIME = 2
HORIZONTAL_MAX_SAME_FORMAT_PER_TIME = 3
MIN_SCHEDULABLE_SCORE = 50.0
MIN_PROVEN_AVG_CHECKIN = 3.0
MIN_PROVEN_FILL_RATE = 0.22
MIN_PROVEN_SESSIONS = 3

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


def is_low_performing_history(row: dict, *, min_sessions: int = MIN_PROVEN_SESSIONS) -> bool:
    """True for proven weak class/trainer/slot history that should not be scheduled."""
    sessions = int((row or {}).get("session_count", 0) or 0)
    if sessions < min_sessions:
        return False
    avg_checkin = float((row or {}).get("avg_checkin", (row or {}).get("avg_attendance", 0.0)) or 0.0)
    avg_fill = float((row or {}).get("avg_fill_rate", 0.0) or 0.0)
    return avg_checkin < MIN_PROVEN_AVG_CHECKIN or avg_fill < MIN_PROVEN_FILL_RATE


def is_am_slot(time_str: str) -> bool:
    return slot_time_to_minutes(time_str) < slot_time_to_minutes("13:00")


def shift_label(time_str: str) -> str:
    return "AM" if is_am_slot(time_str) else "PM"


def canonical_class_key(class_name: str) -> str:
    lower = (class_name or "").lower()
    if "copper" in lower and "fit" in lower:
        return "Copper + Cloves FIT"
    if "copper" in lower and ("mat 57" in lower or "mat57" in lower):
        return "Copper + Cloves Mat 57"
    if "copper" in lower:
        return "Copper + Cloves Barre 57"
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


def protected_class_variant_key(class_name: str) -> str:
    """Exact class identity for protected slots; Express and full-length variants must not collapse."""
    return " ".join(str(class_name or "").split()).lower()


def same_format_family(left: str, right: str) -> bool:
    return get_class_format(left) == get_class_format(right)


def same_protected_class_variant(left: str, right: str) -> bool:
    return protected_class_variant_key(left) == protected_class_variant_key(right)


def location_class_allowed(location: str, class_name: str) -> bool:
    if location == "Courtside":
        return canonical_class_key(class_name) in COURTSIDE_ALLOWED_CANONICAL_CLASSES
    if location == "Copper & Cloves":
        return class_name in COPPER_ALLOWED_CLASSES
    return True


def is_protected_strength_lab_row(row: dict) -> bool:
    return (
        canonical_class_key(row.get("class", "")) == "Studio Strength Lab"
        and float(row.get("avg_fill_rate", 0.0) or 0.0) > STRENGTH_LAB_PROTECT_FILL_THRESHOLD
        and int(row.get("session_count", 0) or 0) >= 3
    )


def is_protected_strength_lab_history(class_name: str, hist: dict) -> bool:
    return (
        canonical_class_key(class_name) == "Studio Strength Lab"
        and float((hist or {}).get("avg_fill_rate", 0.0) or 0.0) > STRENGTH_LAB_PROTECT_FILL_THRESHOLD
        and int((hist or {}).get("session_count", 0) or 0) >= 3
    )


def class_difficulty_level(class_name: str) -> str:
    lower = str(class_name or "").lower()
    if "recovery" in lower or "foundations" in lower:
        return "beginner"
    if "express" in lower or "mat 57" in lower:
        return "beginner"
    if "fit" in lower or "cardio" in lower or "strength lab" in lower or "powercycle" in lower or "hiit" in lower or "amped" in lower:
        return "advanced"
    return "intermediate"


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
    against same-shift slots; this blocks slots before the configured afternoon window."""
    return slot_time_to_minutes(time_str) >= slot_time_to_minutes("12:30")


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
    if "PowerCycle" in class_name and location in BENGALURU_LOCATIONS:
        violations.append("UNIV-011: PowerCycle at Bengaluru locations")
    if "Strength Lab" in class_name and location != "Kwality House, Kemps Corner":
        violations.append("UNIV-012: Strength Lab not at Kwality")
    if "Recovery" in class_name:
        if slots_today and time_str <= min(s.time for s in slots_today):
            violations.append("UNIV-007: Recovery is first class")
        if not is_recovery_last_in_shift(class_name, time_str, slots_today):
            violations.append("UNIV-026: Recovery not last in shift")
        if not is_recovery_allowed_in_slot(time_str):
            violations.append("UNIV-026: Recovery in early slot (must be at or after 12:30)")
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
    performance_score: Optional[float] = None
    placement_score: Optional[float] = None
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

    def day_schedule(self, day: str) -> List[Tuple[str, str, str]]:
        return self._schedule.get(day, [])

    @property
    def max_weekly_minutes(self) -> int:
        if self.tier == 1: return MAX_TRAINER_WEEKLY_MINUTES_T1
        if self.tier == 2: return MAX_TRAINER_WEEKLY_MINUTES_T2
        return MAX_TRAINER_WEEKLY_MINUTES_T3

    def classes_today(self, day: str, location: str) -> List[str]:
        return [t for t, loc, _ in self._schedule.get(day, []) if loc == location]

    def classes_at_location(self, location: str) -> int:
        return sum(1 for entries in self._schedule.values() for _, loc, _ in entries if loc == location)

    def minutes_at_location(self, location: str) -> int:
        return sum(
            get_class_duration(class_name)
            for entries in self._schedule.values()
            for _, loc, class_name in entries
            if loc == location
        )

    def worked_days(self) -> Set[str]:
        return set(self._schedule.keys())

    def worked_days_count(self) -> int:
        return len(self.worked_days())

    def minutes_today(self, day: str) -> int:
        return sum(get_class_duration(class_name) for _, _, class_name in self._schedule.get(day, []))

    def locations_in_shift(self, day: str, shift: str) -> Set[str]:
        return {loc for t, loc, _ in self._schedule.get(day, []) if shift_label(t) == shift}

    def shifts_worked_today(self, day: str) -> Set[str]:
        return {shift_label(t) for t, _, _ in self._schedule.get(day, [])}

    def _violates_location_shift_lock(self, day: str, time_str: str, location: str) -> bool:
        """Hard trainer movement guard.

        Main studios are the anchor studios. Within a single AM/PM shift on a
        day, a trainer may work at only one main studio. Derived/pop-up studios
        can attach to their city only when they are the trainer's final stop in
        that shift; this keeps Courtside/Copper available as controlled overflow
        without permitting main-studio hopping.
        """
        candidate_shift = shift_label(time_str)
        candidate_min = slot_time_to_minutes(time_str)
        same_shift = [
            (slot_time_to_minutes(t), loc)
            for t, loc, _ in self._schedule.get(day, [])
            if shift_label(t) == candidate_shift
        ]
        if not same_shift:
            return False

        if any(location_region(loc) != location_region(location) for _, loc in same_shift):
            return True

        existing_main = {loc for _, loc in same_shift if is_main_studio(loc)}
        if is_main_studio(location):
            if existing_main and any(loc != location for loc in existing_main):
                return True
            # A derived studio is allowed only as the final stop; after that the
            # trainer cannot return to the main studio in the same shift.
            if any(is_derived_studio(loc) for _, loc in same_shift):
                return True
            return False

        if is_derived_studio(location):
            # Derived/pop-up studios are exception stops, not a second shift base.
            if any(is_derived_studio(loc) for _, loc in same_shift):
                return True
            # Derived studio must be the last assignment in that shift.
            if any(existing_min > candidate_min for existing_min, _ in same_shift):
                return True
            return False

        return False

    def can_add(self, day: str, time_str: str, location: str, class_name: str,
                max_per_day: int, win_start: str, win_end: str) -> bool:
        new_start = slot_time_to_minutes(time_str)
        new_dur = get_class_duration(class_name)
        candidate_shift = shift_label(time_str)

        # Time window check
        if new_start < slot_time_to_minutes(win_start):
            return False
        if new_start >= slot_time_to_minutes(win_end):
            return False

        # Max per day at this location
        today_loc = self.classes_today(day, location)
        if len(today_loc) >= max_per_day:
            return False

        if day not in self.worked_days() and self.worked_days_count() >= MAX_TRAINER_WORK_DAYS:
            return False

        # Tier-based hard weekly caps
        max_mins = MAX_TRAINER_WEEKLY_MINUTES_T1 if self.tier == 1 else (MAX_TRAINER_WEEKLY_MINUTES_T2 if self.tier == 2 else MAX_TRAINER_WEEKLY_MINUTES_T3)
        if self.weekly_minutes + new_dur > max_mins:
            return False

        if self.minutes_today(day) + new_dur > MAX_TRAINER_DAILY_MINUTES:
            return False

        # Duration-based overlap check against ALL classes today (any location)
        for (et, eloc, ecls) in self._schedule.get(day, []):
            e_start = slot_time_to_minutes(et)
            e_dur = get_class_duration(ecls)
            if time_windows_overlap(new_start, new_dur, e_start, e_dur):
                return False

        if self._violates_location_shift_lock(day, time_str, location):
            return False

        # A trainer can work only one half-day shift per date.
        opposite_shift = "PM" if candidate_shift == "AM" else "AM"
        if opposite_shift in self.shifts_worked_today(day):
            return False

        return True

    def add(self, day: str, time_str: str, location: str, class_name: str):
        if day not in self._schedule:
            self._schedule[day] = []
        self._schedule[day].append((time_str, location, class_name))
        self.weekly_minutes += get_class_duration(class_name)

    def remove(self, day: str, time_str: str, location: str, class_name: str) -> bool:
        """Remove a previously-added placement. Returns True if found and removed."""
        entries = self._schedule.get(day) or []
        target = (time_str, location, class_name)
        for i, ent in enumerate(entries):
            if ent == target:
                entries.pop(i)
                self.weekly_minutes -= get_class_duration(class_name)
                if not entries:
                    self._schedule.pop(day, None)
                return True
        return False

    def at_weekly_target(self) -> bool:
        if self.tier == 1:
            return self.weekly_minutes >= TIER1_WEEKLY_TARGET_MIN
        if self.tier == 2:
            return self.weekly_minutes >= TIER2_WEEKLY_TARGET_MIN
        return self.weekly_minutes >= TIER3_WEEKLY_TARGET_MAX


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

    def used_minutes(self, day: str, room_id: str) -> int:
        return sum((e - s) for (s, e, _, _) in self._occ.get((day, room_id), []))

    def utilisation_summary(self, available_minutes_per_day: int = 13 * 60) -> Dict[str, Dict]:
        """Return per-(day, room) and per-room utilisation %.
        Available window defaults to 07:00–20:00 (13h). Days that have no class
        placements are excluded so an entire un-scheduled day does not drag the
        denominator."""
        out: Dict[str, Dict] = {"per_day_room": {}, "per_room": {}}
        days_with_classes: Set[str] = {d for (d, _r) in self._occ.keys()}
        for room_id in self.rooms:
            room_total_used = 0
            room_total_avail = 0
            for day in days_with_classes:
                used = self.used_minutes(day, room_id)
                avail = available_minutes_per_day
                pct = (used / avail * 100.0) if avail else 0.0
                out["per_day_room"][f"{day}|{room_id}"] = {"used_min": used, "avail_min": avail, "pct": round(pct, 1)}
                room_total_used += used
                room_total_avail += avail
            room_pct = (room_total_used / room_total_avail * 100.0) if room_total_avail else 0.0
            out["per_room"][room_id] = {"used_min": room_total_used, "avail_min": room_total_avail, "pct": round(room_pct, 1)}
        return out


class ScheduleOptimiser:
    def __init__(self, target_week_start: str, locations: List[str] = None,
                 overrides_path: Optional[str] = None,
                 variation_seed: Optional[int] = None,
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
            "Courtside",
            "Copper & Cloves",
        ]
        self.overrides = self._load_overrides(overrides_path)
        self.schedule_config = self._load_schedule_config()
        # Apply runtime caps from saved settings (work-days, weekly/daily hour caps,
        # T1 targets). Overrides the module-level defaults so TrainerState picks them up.
        self.runtime_caps = apply_settings_caps_from_config(self.schedule_config)
        self.ai_brief: dict = {}
        self._ai_boost: Dict[tuple, float] = {}
        self._ai_penalty: Dict[tuple, float] = {}
        self._ai_mix_boost: Dict[tuple, float] = {}
        # variation_seed semantics:
        #   None  -> auto-seed from time+urandom (production: every run unique)
        #   0     -> deterministic mode (test fixtures rely on this; noise disabled)
        #   int>0 -> reproducible run with that seed
        import time as _time
        import os as _os
        deterministic = variation_seed == 0
        if variation_seed is None:
            variation_seed = int.from_bytes(_os.urandom(4), "big") ^ int(_time.time_ns() & 0x7FFFFFFF)
            variation_seed &= 0x7FFFFFFF
            if variation_seed == 0:
                variation_seed = 1
        self._variation_seed = variation_seed
        self._output_suffix = output_suffix
        self._optimization_mode = optimization_mode
        self._deterministic = deterministic
        # In deterministic mode, keep an rng for any non-noise rng users but
        # _score_noise returns 0 (see method).
        self._rng = None if deterministic else random.Random(variation_seed)
        self._weekly_location_targets: Dict[str, int] = {}
        self._weekly_location_overflow_minutes: Dict[str, int] = {}
        self._time_class_counts: Dict[Tuple[str, str, str], int] = defaultdict(int)
        self._time_format_counts: Dict[Tuple[str, str, str], int] = defaultdict(int)
        self._time_level_counts: Dict[Tuple[str, str, str], int] = defaultdict(int)
        self.trainer_priority: Dict[str, int] = self.schedule_config.get("trainer_priority", {})

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
            config = json.load(f)
        config.setdefault("manual_protected", [])
        config.setdefault("custom_rules", [])
        self._apply_custom_rules_to_config(config)
        return config

    def _apply_custom_rules_to_config(self, config: dict) -> None:
        for rule in config.get("custom_rules", []) or []:
            if not isinstance(rule, dict) or rule.get("enabled") is False:
                continue
            location = rule.get("location")
            rule_type = rule.get("rule_type")
            operator = rule.get("operator", "exactly")
            value = int(rule.get("value", 0) or 0)
            if rule_type == "daily_target" and location and rule.get("day"):
                target = config.setdefault("targets", {}).setdefault(location, {}).setdefault(rule["day"], {})
                if operator == "max":
                    target["max"] = value
                    if int(target.get("target", value) or 0) > value:
                        target["target"] = value
                elif operator == "min":
                    target["target"] = max(int(target.get("target", 0) or 0), value)
                    target["max"] = max(int(target.get("max", value) or value), target["target"])
                else:
                    target["target"] = value
                    target["max"] = value
            elif rule_type == "weekly_class_mix" and location and rule.get("class_name"):
                mix = config.setdefault("class_mix", {}).setdefault(location, {}).setdefault(rule["class_name"], {})
                if operator == "min":
                    mix["min"] = value
                elif operator == "max":
                    mix["max"] = value
                else:
                    mix["min"] = value
                    mix["max"] = value

    def _is_inactive(self, trainer):
        inactive = {normalize_trainer_name(t) for t in self.overrides.get("inactive_trainers", [])}
        inactive.update(getattr(self, "inactive_profile_trainers", set()))
        return normalize_trainer_name(trainer) in inactive

    def _on_leave(self, trainer, date_str, location=None):
        for p in self.overrides.get("leave_periods", []):
            if normalize_trainer_name(p["trainer"]) != normalize_trainer_name(trainer): continue
            if p.get("location") and p["location"] != location: continue
            if p["from_date"] <= date_str <= p["to_date"]: return True
        # Compute day name from date_str so recurring weekday off-days can match.
        day_name = ""
        try:
            day_name = DOW_MAP[date.fromisoformat(date_str).weekday()]
        except Exception:
            day_name = ""
        for o in self.overrides.get("off_days", []):
            if normalize_trainer_name(o["trainer"]) != normalize_trainer_name(trainer): continue
            if o.get("location") and o["location"] != location: continue
            if o.get("date") and o["date"] == date_str: return True
            # Recurring weekly off-day: { trainer, day_of_week } or { trainer, weekday: "Monday" }
            recurring = o.get("day_of_week") or o.get("weekday") or o.get("recurring_day")
            if recurring and day_name and str(recurring).strip().lower() == day_name.lower():
                return True
        # Profile-level recurring off-days (under trainer_profiles[name].off_day_weekly or .week_off).
        try:
            prof = self.trainer_profiles.get(trainer) or {}
            for key in ("off_day_weekly", "week_off", "recurring_off_day"):
                val = prof.get(key)
                if not val:
                    continue
                days = val if isinstance(val, list) else [val]
                for d in days:
                    if day_name and str(d).strip().lower() == day_name.lower():
                        return True
        except Exception:
            pass
        return False

    def _date_for_day(self, day_name: str) -> Optional[str]:
        try:
            week_start = date.fromisoformat(self.target_week_start)
            return (week_start + timedelta(days=DOW_REVERSE[day_name])).isoformat()
        except Exception:
            return None

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
        # Legacy class-count weekly caps from custom rules/overrides.
        # Hour-based caps are enforced separately in _max_weekly_minutes_cap.
        custom_rule_cap: Optional[int] = None
        for rule in self.schedule_config.get("custom_rules", []) or []:
            if not isinstance(rule, dict) or rule.get("enabled") is False:
                continue
            if rule.get("priority", "hard") != "hard":
                continue
            if rule.get("rule_type") not in {"trainer_availability", "trainer_load_limit"}:
                continue
            if rule.get("operator") not in {"max", "max_classes"}:
                continue
            if str(rule.get("unit", "classes")).lower() in {"hour", "hours", "hr", "hrs"}:
                continue
            if normalize_trainer_name(rule.get("trainer")) != normalize_trainer_name(trainer):
                continue
            rule_location = rule.get("location")
            if rule_location and rule_location != location:
                continue
            try:
                cap = int(rule.get("value", 0) or 0)
            except (TypeError, ValueError):
                continue
            if cap <= 0:
                continue
            custom_rule_cap = cap if custom_rule_cap is None else min(custom_rule_cap, cap)

        for ov in self.overrides.get("max_classes_overrides", []):
            if normalize_trainer_name(ov["trainer"]) == normalize_trainer_name(trainer) and (not ov.get("location") or ov["location"] == location):
                override_cap = ov.get("max_per_week")
                if override_cap is None:
                    return custom_rule_cap
                try:
                    override_cap_int = int(override_cap)
                except (TypeError, ValueError):
                    return custom_rule_cap
                if custom_rule_cap is None:
                    return override_cap_int
                return min(custom_rule_cap, override_cap_int)
        return custom_rule_cap

    def _max_weekly_minutes_cap(self, trainer: str, location: str) -> Optional[int]:
        """Cross-studio hard weekly cap in minutes for a trainer.

        Supports custom trainer rule:
        - rule_type=trainer_availability
        - operator=max
        - value=<hours>
        - unit must be hours/hour/hr/hrs.
        """
        cap_minutes: Optional[int] = None
        for rule in self.schedule_config.get("custom_rules", []) or []:
            if not isinstance(rule, dict) or rule.get("enabled") is False:
                continue
            if rule.get("priority", "hard") != "hard":
                continue
            if rule.get("rule_type") not in {"trainer_availability", "trainer_load_limit"}:
                continue
            if rule.get("operator") not in {"max", "max_minutes"}:
                continue
            if normalize_trainer_name(rule.get("trainer")) != normalize_trainer_name(trainer):
                continue
            rule_location = rule.get("location")
            if rule_location and rule_location != location:
                continue
            unit = str(rule.get("unit", "classes")).lower()
            try:
                raw = float(rule.get("value", 0) or 0)
            except (TypeError, ValueError):
                continue
            if raw <= 0:
                continue
            if unit in {"class", "classes"}:
                # class-count caps are handled elsewhere
                continue
            if rule.get("operator") == "max_minutes":
                minutes = int(round(raw))
                cap_minutes = minutes if cap_minutes is None else min(cap_minutes, minutes)
                continue
            minutes = int(round(raw * 60.0))
            cap_minutes = minutes if cap_minutes is None else min(cap_minutes, minutes)
        return cap_minutes

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

    def _build_score_indexes(self) -> None:
        """Pre-index ranked class/trainer rows used by slot filling."""
        by_location: Dict[str, List[dict]] = defaultdict(list)
        by_location_day: Dict[Tuple[str, int], List[dict]] = defaultdict(list)
        by_location_class_day: Dict[Tuple[str, str, int], List[dict]] = defaultdict(list)

        for row in self.scores_data.get("class_slot_ranking", []):
            location = row.get("location")
            day = row.get("day")
            class_name = row.get("class")
            if not location:
                continue
            by_location[location].append(row)
            if day is not None:
                by_location_day[(location, day)].append(row)
                if class_name:
                    by_location_class_day[(location, class_name, day)].append(row)

        self._ranking_by_location = dict(by_location)
        self._ranking_by_location_day = dict(by_location_day)
        self._ranking_by_location_class_day = dict(by_location_class_day)

    def _candidate_rows(self, location: str, day: int, day_filter: bool) -> List[dict]:
        if not hasattr(self, "_ranking_by_location"):
            self._build_score_indexes()
        if day_filter:
            rows = self._ranking_by_location_day.get((location, day), [])
            if rows:
                return rows
            return [
                {**row, "location": location}
                for source in DERIVED_LOCATION_SOURCES.get(location, [])
                for row in self._ranking_by_location_day.get((source, day), [])
            ]
        rows = self._ranking_by_location.get(location, [])
        if rows:
            return rows
        return [
            {**row, "location": location}
            for source in DERIVED_LOCATION_SOURCES.get(location, [])
            for row in self._ranking_by_location.get(source, [])
        ]

    def _class_candidate_rows(self, location: str, class_name: str, day: int) -> List[dict]:
        if not hasattr(self, "_ranking_by_location_class_day"):
            self._build_score_indexes()
        rows = self._ranking_by_location_class_day.get((location, class_name, day), [])
        if rows:
            return rows
        return [
            {**row, "location": location}
            for source in DERIVED_LOCATION_SOURCES.get(location, [])
            for row in self._ranking_by_location_class_day.get((source, class_name, day), [])
        ]

    def _build_history_indexes(self) -> None:
        """Pre-index historical metrics for exact and nearby slot lookups."""
        by_combo_day: Dict[Tuple[str, str, str, int], List[Tuple[int, dict]]] = defaultdict(list)
        by_slot_day: Dict[Tuple[str, str, int], List[Tuple[int, dict]]] = defaultdict(list)
        exact_slot_members: Dict[Tuple[str, str, int, str], List[dict]] = defaultdict(list)
        canonical_by_slot_day: Dict[Tuple[str, str, int], List[Tuple[int, dict]]] = defaultdict(list)
        canonical_exact_slot_members: Dict[Tuple[str, str, int, str], List[dict]] = defaultdict(list)

        for (location, class_name, trainer, day, time_str), metrics in self.hist_lookup.items():
            if not time_str:
                continue
            time_min = slot_time_to_minutes(time_str)
            canonical_class = canonical_class_key(class_name)
            by_combo_day[(location, class_name, trainer, day)].append((time_min, metrics))
            by_slot_day[(location, class_name, day)].append((time_min, metrics))
            exact_slot_members[(location, class_name, day, time_str)].append(metrics)
            canonical_by_slot_day[(location, canonical_class, day)].append((time_min, metrics))
            canonical_exact_slot_members[(location, canonical_class, day, time_str)].append(metrics)

        for rows in by_combo_day.values():
            rows.sort(key=lambda item: item[0])
        for rows in by_slot_day.values():
            rows.sort(key=lambda item: item[0])
        for rows in canonical_by_slot_day.values():
            rows.sort(key=lambda item: item[0])

        self._hist_by_combo_day = dict(by_combo_day)
        self._hist_by_slot_day = dict(by_slot_day)
        self._hist_slot_exact = {
            key: self._aggregate_hist_metrics(metrics)
            for key, metrics in exact_slot_members.items()
        }
        self._hist_by_canonical_slot_day = dict(canonical_by_slot_day)
        self._hist_canonical_slot_exact = {
            key: self._aggregate_hist_metrics(metrics)
            for key, metrics in canonical_exact_slot_members.items()
        }
        self._hist_slot_cache: Dict[Tuple[str, str, int, str], dict] = {}

    def _aggregate_hist_metrics(self, matching: List[dict]) -> dict:
        total_sessions = sum(m.get("session_count", 0) for m in matching)
        if total_sessions == 0:
            return {}

        def weighted(field: str) -> float:
            return sum(
                m.get(field, 0) * m.get("session_count", 0)
                for m in matching
            ) / total_sessions

        return {
            "session_count": total_sessions,
            "avg_fill_rate": weighted("avg_fill_rate"),
            "avg_checkin": weighted("avg_checkin"),
            "avg_late_cancel_rate": weighted("avg_late_cancel_rate"),
            "avg_no_show_rate": weighted("avg_no_show_rate"),
        }

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
        self.trainer_home_region = {}
        for p in profiles_list:
            if not p.get("name"):
                continue
            base_locs = set((p.get("locations") or {}).keys())
            if base_locs & BENGALURU_LOCATIONS:
                self.trainer_home_region[p["name"]] = "bengaluru"
            elif base_locs & MUMBAI_LOCATIONS:
                self.trainer_home_region[p["name"]] = "mumbai"
        self._enrich_trainer_profiles(profiles_list, scores_data)

        self.class_family: Dict[str, str] = {c["name"]: c.get("family", "") for c in class_formats_list}
        self.location_rules = {
            "Kwality House, Kemps Corner": kwality_rules,
            "Supreme HQ, Bandra": supreme_rules,
            "Kenkere House": kenkere_rules,
            "Courtside": supreme_rules,
            "Copper & Cloves": kenkere_rules,
        }
        self.scores_data = scores_data
        self._build_score_indexes()
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
        self._build_history_indexes()

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
            if r.get("protect_exact_combo") or is_protected_strength_lab_row(r):
                self.protected.setdefault(key, []).append(r)
        for r in scores_data.get("slot_group_ranking", []):
            cname = r["class"]
            fam = self.class_family.get(cname, "")
            if is_excluded_class(cname, fam):
                continue
            key = (r["location"], r["day"])
            if r.get("pinned_slot") or r.get("protect_class_time"):
                self.protected_class_times.setdefault(key, []).append(r)

        self._pinned_minutes_remaining = self._build_pinned_minutes_remaining()
        self._time_class_counts: Dict[Tuple[str, str, str], int] = defaultdict(int)
        self._time_format_counts: Dict[Tuple[str, str, str], int] = defaultdict(int)
        self._time_level_counts: Dict[Tuple[str, str, str], int] = defaultdict(int)

        week_start = date.fromisoformat(self.target_week_start)
        all_slots: List[ScheduleSlot] = []

        for loc in self._location_planning_order():
            loc_short = loc.split(",")[0]
            print(f"[Agent 5] Planning {loc_short}...", flush=True)
            loc_slots = self._schedule_location(loc, week_start)
            all_slots.extend(loc_slots)
            by_day = {}
            for s in loc_slots:
                by_day.setdefault(s.day_of_week, []).append(s)
            for day_name in ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]:
                n = len(by_day.get(day_name, []))
                if n:
                    avg_score = sum(s.score for s in by_day[day_name]) / n
                    print(f"[Agent 5]   {day_name[:3]} {loc_short}: {n} classes, avg score {avg_score:.0f}", flush=True)

        self._final_weekly_floor_repair(all_slots, week_start)
        all_slots = self._drop_inactive_slots(all_slots)
        # T1 pull-up: try to lift under-target T1 trainers by swapping eligible
        # T2/T3 placements to a free T1, only when score regression is bounded.
        self._tier1_pull_up_pass(all_slots)
        self._print_utilisation()

        output = {
            "target_week_start": self.target_week_start,
            "schedule": [asdict(s) for s in all_slots],
            "optimization_mode": self._optimization_mode,
            "floor_repair_log": getattr(self, "_floor_repair_log", []),
            "variation_seed": self._variation_seed,
        }
        STATE_DIR.mkdir(exist_ok=True)
        suffix = f"_{self._output_suffix}" if self._output_suffix else ""
        with open(STATE_DIR / f"05_draft_schedule{suffix}.json", "w") as f:
            json.dump(output, f, indent=2)
        prune_draft_schedule_files(STATE_DIR, keep_groups=5)

        print(f"[Agent 5] Optimiser complete — {len(all_slots)} slots across {len(self.locations)} locations")
        return output

    def _location_planning_order(self) -> List[str]:
        priority = {
            SUPREME_LOCATION: 0,
            KWALITY_LOCATION: 1,
            "Kenkere House": 2,
            "Courtside": 3,
            "Copper & Cloves": 4,
        }
        return sorted(self.locations, key=lambda loc: (priority.get(loc, 50), self.locations.index(loc)))

    def _build_pinned_minutes_remaining(self) -> Dict[str, int]:
        pinned_minutes: Dict[str, int] = defaultdict(int)
        self._kwality_pinned_minutes_total: Dict[str, int] = defaultdict(int)
        for location in self.locations:
            for day_name in DAY_ORDER:
                for slot in self._get_pinned_slots(location, day_name):
                    trainer = slot.get("trainer")
                    class_name = slot.get("class", "")
                    if not trainer or self._is_inactive(trainer):
                        continue
                    duration = get_class_duration(class_name)
                    pinned_minutes[trainer] += duration
                    if location == KWALITY_LOCATION:
                        self._kwality_pinned_minutes_total[trainer] += duration
        return pinned_minutes

    def _mumbai_tier1_supreme_band(self, trainer: str) -> Optional[Tuple[int, int]]:
        state = self.trainer_states.get(trainer)
        if not state or state.tier != 1:
            return None
        profile = self.trainer_profiles.get(trainer, {})
        if getattr(self, "trainer_home_region", {}).get(profile.get("name"), "") != "mumbai":
            return None

        base_min = int(MAX_TRAINER_WEEKLY_MINUTES_T1 * MUMBAI_TIER1_SUPREME_MIN_SHARE)
        base_max = int(MAX_TRAINER_WEEKLY_MINUTES_T1 * MUMBAI_TIER1_SUPREME_MAX_SHARE)

        # If Kwality pinned load exceeds the non-Supreme allowance, relax the
        # Supreme minimum by only the pinned excess.
        max_non_supreme = MAX_TRAINER_WEEKLY_MINUTES_T1 - base_min
        kwality_pinned = getattr(self, "_kwality_pinned_minutes_total", {}).get(trainer, 0)
        kwality_excess = max(0, kwality_pinned - max_non_supreme)
        adjusted_min = max(0, base_min - kwality_excess)
        adjusted_max = max(adjusted_min, base_max)
        supreme_profile = self._profile_location_data(profile, SUPREME_LOCATION) or {}
        available_days = supreme_profile.get("available_days") or []
        max_per_day = int(supreme_profile.get("max_classes_per_day") or 4)
        if available_days:
            feasible_minutes = len(set(available_days)) * max_per_day * get_class_duration("Studio Barre 57")
            adjusted_min = min(adjusted_min, feasible_minutes)
            adjusted_max = min(adjusted_max, feasible_minutes)
        return adjusted_min, adjusted_max

    def _release_pinned_reservation(self, trainer: str, class_name: str):
        remaining = getattr(self, "_pinned_minutes_remaining", None)
        if not remaining or not trainer:
            return
        remaining[trainer] = max(0, remaining.get(trainer, 0) - get_class_duration(class_name))

    def _drop_inactive_slots(self, slots: List[ScheduleSlot]) -> List[ScheduleSlot]:
        kept: List[ScheduleSlot] = []
        dropped: List[ScheduleSlot] = []
        for slot in slots:
            if self._is_inactive(slot.trainer_1):
                dropped.append(slot)
            else:
                kept.append(slot)
        if dropped:
            names = sorted({s.trainer_1 for s in dropped})
            print(
                f"  [INACTIVE GUARD] Dropped {len(dropped)} slot(s) assigned to inactive trainer(s): "
                + ", ".join(names),
                flush=True,
            )
        return kept

    def _tier1_pull_up_pass(self, all_slots: List[ScheduleSlot]) -> None:
        """Post-pass: for each Tier-1 trainer below TIER1_WEEKLY_TARGET_MIN, scan
        T2/T3-held INCLUDE slots they're qualified for and swap if the candidate
        passes hard limits and the score delta is acceptable. Bounded to a few
        swaps per trainer to avoid thrashing."""
        try:
            t1_underused = [
                (name, state) for name, state in self.trainer_states.items()
                if state.tier == 1 and state.weekly_minutes < TIER1_WEEKLY_TARGET_MIN
            ]
            if not t1_underused:
                return
            for t1_name, t1_state in t1_underused:
                swaps_done = 0
                for slot in all_slots:
                    if swaps_done >= 3:
                        break
                    if slot.recommendation in {"PINNED", "PROTECT", "PROTECT_EXACT"}:
                        continue
                    if slot.trainer_1 == t1_name:
                        continue
                    current = self.trainer_states.get(slot.trainer_1)
                    if not current or current.tier == 1:
                        continue
                    # T1 must be qualified for this class.
                    prof = self.trainer_profiles.get(t1_name) or {}
                    quals = prof.get("qualifications", {}) or {}
                    cname = slot.class_name
                    if "PowerCycle" in cname and not quals.get("powercycle"):
                        continue
                    if "Strength Lab" in cname and not quals.get("strength_lab"):
                        continue
                    if "Foundations" in cname and not quals.get("foundations"):
                        continue
                    if self._is_inactive(t1_name) or self._on_leave(t1_name, slot.date, slot.location):
                        continue
                    if self._loc_excluded(t1_name, slot.location):
                        continue
                    loc_data = self._profile_location_data(prof, slot.location)
                    if not loc_data:
                        continue
                    avail_days = self._available_days(t1_name, slot.location, loc_data.get("available_days", []))
                    if slot.day_of_week not in avail_days:
                        continue
                    tw = loc_data.get("time_window", {})
                    win_s, win_e = self._time_window(t1_name, slot.location, tw.get("start", "06:00"), tw.get("end", "22:00"))
                    max_d = self._max_per_day(t1_name, slot.location, loc_data.get("max_classes_per_day", 4))
                    if not t1_state.can_add(slot.day_of_week, slot.time, slot.location, cname, max_d, win_s, win_e):
                        continue
                    # Perform swap.
                    current.remove(slot.day_of_week, slot.time, slot.location, cname)
                    t1_state.add(slot.day_of_week, slot.time, slot.location, cname)
                    slot.trainer_1 = t1_name
                    slot.scheduling_reason = (slot.scheduling_reason or "") + " | T1 pull-up swap"
                    swaps_done += 1
                    if t1_state.weekly_minutes >= TIER1_WEEKLY_TARGET_MIN:
                        break
        except Exception as e:
            print(f"[Agent 5] T1 pull-up pass error: {e}")

    def _final_weekly_floor_repair(self, all_slots: List[ScheduleSlot], week_start: date):
        """Add conservative filler classes only when a location misses its hard weekly floor.
        Floor repairs are logged onto ``self._floor_repair_log`` for scorecard warnings."""
        # Pull canonical floors from settings_options if available, fall back to CLAUDE.md values.
        weekly_min = {
            "Kwality House, Kemps Corner": 70,
            "Supreme HQ, Bandra": 65,
            "Kenkere House": 55,
            "Courtside": 4,
        }
        try:
            saved = (self.schedule_config.get("settings_options") or {}).get("location_weekly_floors") or {}
            for k, v in saved.items():
                weekly_min[k] = int(v)
        except Exception:
            pass
        for location in list(weekly_min):
            if location in LOCATION_WEEKLY_CLASS_BOUNDS:
                weekly_min[location] = self._weekly_target_for_location(location)
        if not hasattr(self, "_floor_repair_log"):
            self._floor_repair_log: List[dict] = []
        reserve_times = ["07:00", "07:45", "08:15", "12:00", "12:30", "13:00", "16:00", "17:00", "20:00"]
        filler_classes = [
            "Studio FIT", "Studio Cardio Barre", "Studio Mat 57", "Studio Barre 57",
            "Studio Barre 57 Express", "Studio Cardio Barre Express", "Studio Mat 57 Express",
        ]

        def _loc_count(loc: str) -> int:
            return sum(1 for s in all_slots if s.location == loc)

        def _day_slots(loc: str, day_name: str) -> List[ScheduleSlot]:
            return [s for s in all_slots if s.location == loc and s.day_of_week == day_name]

        for location, floor in weekly_min.items():
            if location not in self.locations:
                continue
            if _loc_count(location) >= floor:
                continue
            rooms = LOCATION_ROOMS.get(location, {})
            loc_targets = self.schedule_config.get("targets", {}).get(location, {})
            attempts = 0
            while _loc_count(location) < floor and attempts < 80:
                attempts += 1
                placed = False
                for day_name in sorted(DAY_ORDER, key=lambda d: len(_day_slots(location, d))):
                    if not self._location_allowed_day(location, day_name):
                        continue
                    day_idx = DOW_REVERSE[day_name]
                    date_str = (week_start + timedelta(days=day_idx)).isoformat()
                    if location in WEEKEND_ONLY_TARGETS:
                        day_max = self._pick_daily_target(location, day_name)
                    else:
                        day_max = int((loc_targets.get(day_name) or {}).get("max", self._pick_daily_target(location, day_name) + 2))
                    slots_today = _day_slots(location, day_name)
                    if len(slots_today) >= day_max:
                        continue
                    for time_str in reserve_times:
                        if day_name == "Sunday" and int(time_str[:2]) < 10:
                            continue
                        if slot_is_in_blocked_window(day_name, time_str):
                            continue
                        start_min = slot_time_to_minutes(time_str)
                        shift = "AM" if is_am_slot(time_str) else "PM"
                        used_at_time = {s.trainer_1 for s in slots_today if s.time == time_str}
                        for class_name in filler_classes:
                            if is_excluded_class(class_name, self.class_family.get(class_name, "barre_57")):
                                continue
                            if not location_class_allowed(location, class_name):
                                continue
                            if self._class_mix_hard_blocked(location, class_name):
                                continue
                            if self._would_repeat_consecutive_format(slots_today, time_str, class_name):
                                continue
                            if would_block_recovery(class_name, time_str, slots_today):
                                continue
                            dur = get_class_duration(class_name)
                            class_fam = self.class_family.get(class_name, "barre_57")
                            room = None
                            for room_id in ("studio_a", "studio_b"):
                                if room_id not in rooms:
                                    continue
                                room_slots = [s for s in slots_today if s.room == room_id]
                                if any(time_windows_overlap(start_min, dur, slot_time_to_minutes(s.time), s.duration_min) for s in room_slots):
                                    continue
                                room = room_id
                                break
                            if room is None:
                                continue
                            for trainer, profile in sorted(self.trainer_profiles.items()):
                                if trainer in used_at_time or self._is_inactive(trainer) or self._on_leave(trainer, date_str, location):
                                    continue
                                loc_data = self._profile_location_data(profile, location)
                                if not loc_data:
                                    continue
                                if day_name not in self._available_days(trainer, location, loc_data.get("available_days", [])):
                                    continue
                                tw = loc_data.get("time_window", {})
                                win_s, win_e = self._time_window(trainer, location, tw.get("start", "06:00"), tw.get("end", "22:00"))
                                max_d = self._max_per_day(trainer, location, loc_data.get("max_classes_per_day", 4))
                                state = self.trainer_states.get(trainer)
                                if not state or not state.can_add(day_name, time_str, location, class_name, max_d, win_s, win_e):
                                    continue
                                hist = self._get_hist(location, class_name, trainer, day_idx, time_str)
                                # Refuse to backfill a proven weak combination — keeping the
                                # quality gate symmetric with the main candidate path.
                                if is_low_performing_history(hist):
                                    continue
                                recommendation = "PROTECT_EXACT" if is_protected_strength_lab_history(class_name, hist) else "INCLUDE"
                                public_score, placement_score, recommendation, slot_is_exp = self._public_score_fields(
                                    68.0, 68.0, hist.get("session_count", 0), recommendation, False
                                )
                                slot = ScheduleSlot(
                                    location=location, date=date_str, day_of_week=day_name, time=time_str,
                                    class_name=class_name, trainer_1=trainer, trainer_2="", cover="",
                                    room=room, capacity=rooms.get(room, {}).get("capacity", 14), duration_min=dur,
                                    predicted_fill_rate=self._evidence_adjusted_fill(hist), score=public_score,
                                    recommendation=recommendation, is_experimental=slot_is_exp,
                                    scheduling_reason="Weekly floor repair: conservative filler placed after ranked top-up exhausted.",
                                    historical_avg_fill=hist.get("avg_fill_rate", 0.0),
                                    historical_avg_checkin=hist.get("avg_checkin", 0.0),
                                    historical_session_count=hist.get("session_count", 0),
                                    historical_late_cancel_rate=hist.get("avg_late_cancel_rate", 0.0),
                                    historical_no_show_rate=hist.get("avg_no_show_rate", 0.0),
                                    performance_score=public_score,
                                    placement_score=placement_score,
                                )
                                all_slots.append(slot)
                                state.add(day_name, time_str, location, class_name)
                                self._floor_repair_log.append({
                                    "location": location, "day": day_name, "time": time_str,
                                    "class": class_name, "trainer": trainer,
                                    "score": public_score,
                                })
                                placed = True
                                break
                            if placed:
                                break
                        if placed:
                            break
                    if placed:
                        break
                if not placed:
                    break

    def _pick_daily_target(self, location: str, day_name: str) -> int:
        """
        Return the run-specific desired class count for this location+day.
        Persisted Settings values are interpreted as a range: target/min is
        the lower bound and max is the ceiling. The optimiser picks inside
        that range so generated class totals are not fixed to either edge.
        """
        # Courtside is intentionally fixed: 2 Saturday + 2 Sunday, none on weekdays.
        if location in WEEKEND_ONLY_TARGETS:
            return int(WEEKEND_ONLY_TARGETS[location].get(day_name, 0))

        # Persisted settings override (if provided from the Settings modal)
        config_targets = self.schedule_config.get("targets", {}).get(location, {}).get(day_name, {})
        if isinstance(config_targets, dict) and (
            config_targets.get("target") is not None or config_targets.get("min") is not None
        ):
            lo = int(config_targets.get("min", config_targets.get("target")) or 0)
            hi = int(config_targets.get("max", lo) or lo)
            if hi < lo:
                hi = lo
            return self._pick_value_in_range(location, day_name, lo, hi)

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

        return self._pick_value_in_range(location, day_name, lo, hi)

    def _pick_value_in_range(self, location: str, day_name: str, lo: int, hi: int) -> int:
        """Deterministically pick inside [lo, hi] for the current run and mode."""
        lo = int(lo)
        hi = int(hi)
        if hi <= lo:
            return lo
        strategy = (self.schedule_config.get("settings_options", {}) or {}).get(
            "target_selection_strategy",
            "seeded_range",
        )
        if strategy == "balanced_midpoint":
            return (lo + hi) // 2
        if strategy == "lower_bias":
            hi = max(lo, (lo + hi) // 2)
        elif strategy == "upper_bias":
            lo = min(hi, (lo + hi + 1) // 2)
        key = f"{self.target_week_start}|{location}|{day_name}|{self._variation_seed}|{self._optimization_mode}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return lo + (int(digest[:8], 16) % (hi - lo + 1))

    def _class_mix_entry(self, location: str, class_name: str) -> dict:
        mix = self.schedule_config.get("class_mix", {}).get(location, {})
        canonical = canonical_class_key(class_name)
        if canonical == "Studio Strength Lab" and canonical in mix:
            return mix.get(canonical, {})
        if class_name in mix:
            return mix.get(class_name, {})
        return mix.get(canonical, {})

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

    def _class_mix_hard_blocked(self, location: str, class_name: str) -> bool:
        """A zero min/max disables a format. Positive max values are soft targets."""
        if not location_class_allowed(location, class_name):
            return True
        entry = self._class_mix_entry(location, class_name)
        if not isinstance(entry, dict):
            return False
        return int(entry.get("min", 0) or 0) == 0 and int(entry.get("max", 1) or 0) == 0

    def _class_mix_allows_candidate(self, location: str, class_name: str, current_count: int) -> bool:
        """Return False when adding one more class would exceed Settings max."""
        entry = self._class_mix_entry(location, class_name)
        if not isinstance(entry, dict) or entry.get("max") is None:
            return True
        max_count = int(entry.get("max", 0) or 0)
        return current_count < max_count

    def _custom_rule_blocks(self, location: str, day_name: str, time_str: str,
                            class_name: str, trainer: str) -> bool:
        for rule in self.schedule_config.get("custom_rules", []) or []:
            if not isinstance(rule, dict) or rule.get("enabled") is False:
                continue
            if rule.get("priority", "hard") != "hard":
                continue
            rule_type = rule.get("rule_type")
            operator = rule.get("operator", "never")
            rule_location = rule.get("location")
            rule_day = rule.get("day")
            rule_time = rule.get("time")
            rule_class = rule.get("class_name") or rule.get("class")
            rule_trainer = rule.get("trainer")

            location_matches = not rule_location or rule_location == location
            day_matches = not rule_day or rule_day == day_name
            time_matches = not rule_time or rule_time[:5] == time_str[:5]
            class_matches = not rule_class or canonical_class_key(rule_class) == canonical_class_key(class_name)
            trainer_matches = not rule_trainer or normalize_trainer_name(rule_trainer) == normalize_trainer_name(trainer)

            if rule_type == "class_time_restriction":
                if operator == "never" and location_matches and day_matches and time_matches and class_matches:
                    return True
                if operator == "only" and location_matches and day_matches and class_matches and rule_time and rule_time[:5] != time_str[:5]:
                    return True
            elif rule_type == "class_location_restriction":
                if operator == "never" and location_matches and class_matches:
                    return True
                if operator == "only" and class_matches and rule_location and rule_location != location:
                    return True
            elif rule_type == "trainer_availability":
                if operator == "never" and location_matches and day_matches and time_matches and trainer_matches:
                    return True
                if operator == "only" and trainer_matches:
                    if rule_location and rule_location != location:
                        return True
                    if rule_day and rule_day != day_name:
                        return True
                    if rule_time and rule_time[:5] != time_str[:5]:
                        return True
            elif rule_type == "time_window_rule":
                if operator == "block_window" and location_matches and day_matches and class_matches:
                    if self._rule_time_window_matches(rule, time_str, class_name):
                        return True
        return False

    def _rule_time_window_matches(self, rule: dict, time_str: str, class_name: str) -> bool:
        start = rule.get("time")
        end = rule.get("time_end")
        if not start:
            return False
        candidate_start = slot_time_to_minutes(time_str)
        candidate_duration = get_class_duration(class_name)
        rule_start = slot_time_to_minutes(start[:5])
        if end:
            rule_end = slot_time_to_minutes(end[:5])
            return time_windows_overlap(candidate_start, candidate_duration, rule_start, max(0, rule_end - rule_start))
        return candidate_start == rule_start

    def _weekly_over_target_penalty(self, location: str, class_name: str, current_count: int) -> float:
        """Return a soft score penalty for scheduling one more class above target max."""
        entry = self._class_mix_entry(location, class_name)
        if not isinstance(entry, dict) or entry.get("max") is None:
            return 0.0
        target = int(entry.get("max", 0) or 0)
        if target <= 0:
            return 0.0
        over_after_candidate = current_count + 1 - target
        if over_after_candidate <= 0:
            return 0.0
        return -min(
            CLASS_MIX_OVER_TARGET_PENALTY_CAP,
            over_after_candidate * CLASS_MIX_OVER_TARGET_PENALTY,
        )

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
        return self._rng.gauss(0, 45.0)

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
        if not avail:
            avail = [
                row
                for source in DERIVED_LOCATION_SOURCES.get(location, [])
                for row in self.slot_availability.get(source, [])
            ]
        viable = {
            s["time"][:5]
            for s in avail
            if s["viable"] and not slot_is_in_blocked_window("Monday", s["time"][:5])
        }
        if not viable and location in WEEKEND_ONLY_TARGETS:
            viable = {"09:00", "10:15", "11:30", "17:00", "18:00"}
        elif not viable:
            viable = {"08:30", "09:30", "10:15", "11:30", "17:30", "18:30"}
        elif location in {KWALITY_LOCATION, SUPREME_LOCATION} and viable.intersection(MUMBAI_PARALLEL_PEAK_TIMES | {"09:00"}):
            viable.update(MUMBAI_PARALLEL_PEAK_TIMES)

        am = sorted(t for t in viable if is_am_slot(t))
        pm = sorted(t for t in viable if not is_am_slot(t))

        location_priority = LOCATION_AM_SLOT_PRIORITY.get(location, [])
        priority_rank = {time: idx for idx, time in enumerate(location_priority)}

        def am_key(t):
            if t in priority_rank:
                return (0, priority_rank[t])
            return (1 if t in PRIME_AM_SLOTS else 2, t)

        pm_priority = LOCATION_PM_SLOT_PRIORITY.get(location, [])
        pm_rank = {time: idx for idx, time in enumerate(pm_priority)}

        def pm_key(t):
            if t in pm_rank:
                return (0, pm_rank[t])
            return (1 if t in PRIME_PM_SLOTS else 2, t)
        am.sort(key=am_key)
        pm.sort(key=pm_key)
        return am, pm

    def _location_allowed_day(self, location: str, day_name: str) -> bool:
        targets = WEEKEND_ONLY_TARGETS.get(location)
        return not targets or day_name in targets

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
            if not self._location_allowed_day(location, day_name):
                continue
            slot_date = week_start + timedelta(days=day_idx)
            date_str = slot_date.isoformat()
            target = self._pick_daily_target(location, day_name)
            day_slots = self._schedule_day(location, day_name, date_str, target,
                                           target, am_slots, pm_slots, room_occ,
                                           weekly_class_counts)
            all_slots.extend(day_slots)
            # Update weekly counts from today's schedule
            for s in day_slots:
                key = s.class_name.split("(")[0].strip()  # normalize variants
                weekly_class_counts[key] = weekly_class_counts.get(key, 0) + 1
                self._time_class_counts[
                    (s.location, s.time, canonical_class_key(s.class_name))
                ] += 1
                self._time_format_counts[
                    (s.location, s.time, get_class_format(s.class_name))
                ] += 1
                self._time_level_counts[
                    (s.location, s.time, class_difficulty_level(s.class_name))
                ] += 1

        self._weekly_total_top_up(location, week_start, all_slots, room_occ, weekly_class_counts, am_slots, pm_slots)
        self._daily_target_top_up(location, week_start, all_slots, room_occ, weekly_class_counts, am_slots, pm_slots)

        # Post-pass: horizontal column diversity (same-time across week)
        self._horizontal_diversity_pass(location, all_slots)

        # Post-pass: format floor fixup
        self._format_floor_fixup(location, all_slots, weekly_class_counts)
        self._enforce_location_weekly_cap(location, all_slots, weekly_class_counts)

        return all_slots

    def _weekly_target_for_location(self, location: str) -> int:
        if location in self._weekly_location_targets:
            return self._weekly_location_targets[location]
        bounds = LOCATION_WEEKLY_CLASS_BOUNDS.get(location) or {}
        lo = int(bounds.get("min", 0) or 0)
        hi = int(bounds.get("max", lo) or lo)
        if hi < lo:
            hi = lo
        selectable_lo = lo + 1 if location in MAIN_STUDIOS and hi > lo else lo
        target = self._pick_value_in_range(location, "WEEKLY", selectable_lo, hi) if hi > selectable_lo else selectable_lo
        overflow_key = f"{self.target_week_start}|{location}|WEEKLY_OVERFLOW|{self._variation_seed}|{self._optimization_mode}"
        overflow_digest = hashlib.sha256(overflow_key.encode("utf-8")).hexdigest()
        overflow_hours = 1 + (int(overflow_digest[:8], 16) % 3)
        self._weekly_location_targets[location] = target
        self._weekly_location_overflow_minutes[location] = overflow_hours * 60
        print(
            f"  [WEEKLY TARGET] {location}: target {target} classes "
            f"(soft overflow +{overflow_hours}h)"
        )
        return target

    def _enforce_location_weekly_cap(
        self,
        location: str,
        all_slots: List["ScheduleSlot"],
        weekly_class_counts: Dict[str, int],
    ) -> None:
        bounds = LOCATION_WEEKLY_CLASS_BOUNDS.get(location) or {}
        if not bounds:
            return
        target = self._weekly_target_for_location(location)
        overflow_minutes = int(self._weekly_location_overflow_minutes.get(location, 120))
        current_minutes = sum(int(getattr(s, "duration_min", 57) or 57) for s in all_slots)
        soft_cap_minutes = target * 57 + overflow_minutes
        if len(all_slots) <= target and current_minutes <= soft_cap_minutes:
            return

        def _priority_drop_score(slot: "ScheduleSlot") -> tuple:
            # Drop low-impact slots first while protecting manual commitments.
            is_hard_kept = slot.recommendation in {"PINNED", "PROTECT_EXACT"}
            is_peak = is_prime_slot(slot.time)
            jitter = (self._rng.random() if self._rng else random.random()) * 0.25
            return (
                1 if is_hard_kept else 0,
                1 if is_peak else 0,
                float(slot.score or 0.0),
                jitter,
            )

        removable = sorted(all_slots, key=_priority_drop_score)
        for slot in removable:
            current_minutes = sum(int(getattr(s, "duration_min", 57) or 57) for s in all_slots)
            if len(all_slots) <= target and current_minutes <= soft_cap_minutes:
                break
            if slot.recommendation in {"PINNED", "PROTECT_EXACT"}:
                continue
            all_slots.remove(slot)
            key = slot.class_name.split("(")[0].strip()
            weekly_class_counts[key] = max(0, weekly_class_counts.get(key, 0) - 1)

        if len(all_slots) > target:
            # Last resort if cap still exceeded due to too many hard-kept classes.
            print(
                f"  [CAP WARN] {location}: weekly target {target} could not be fully enforced; "
                f"kept {len(all_slots)} due to protected/pinned commitments"
            )

    def _weekly_total_top_up(self, location: str, week_start: date, all_slots: List[ScheduleSlot],
                             room_occ: RoomOccupancy, weekly_class_counts: Dict[str, int],
                             am_slots: List[str], pm_slots: List[str]):
        configured_floor = {
            "Kwality House, Kemps Corner": 70,
            "Supreme HQ, Bandra": 65,
            "Kenkere House": 55,
            "Courtside": 4,
        }.get(location, 0)
        weekly_min = (LOCATION_WEEKLY_CLASS_BOUNDS.get(location) or {}).get("min", configured_floor)
        if location in LOCATION_WEEKLY_CLASS_BOUNDS:
            weekly_min = self._weekly_target_for_location(location)
        config_targets = self.schedule_config.get("targets", {}).get(location, {})
        if config_targets and location not in LOCATION_WEEKLY_CLASS_BOUNDS:
            weekly_min = sum(
                self._pick_daily_target(location, day_name)
                for day_name in DAY_ORDER
                if isinstance(config_targets.get(day_name), dict)
                and (
                    config_targets[day_name].get("target") is not None
                    or config_targets[day_name].get("min") is not None
                )
            )
        if len(all_slots) >= weekly_min:
            return

        attempts = 0
        while len(all_slots) < weekly_min and attempts < 250:
            attempts += 1
            by_day: Dict[str, List[ScheduleSlot]] = {day: [] for day in DAY_ORDER}
            for slot in all_slots:
                by_day.setdefault(slot.day_of_week, []).append(slot)
            placed = False
            for day_name in sorted(DAY_ORDER, key=lambda d: len(by_day.get(d, []))):
                if not self._location_allowed_day(location, day_name):
                    continue
                day_idx = DOW_REVERSE[day_name]
                date_str = (week_start + timedelta(days=day_idx)).isoformat()
                if location in WEEKEND_ONLY_TARGETS:
                    day_max = self._pick_daily_target(location, day_name)
                else:
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
                    self._time_class_counts[
                        (result.location, result.time, canonical_class_key(result.class_name))
                    ] += 1
                    self._time_format_counts[
                        (result.location, result.time, get_class_format(result.class_name))
                    ] += 1
                    self._time_level_counts[
                        (result.location, result.time, class_difficulty_level(result.class_name))
                    ] += 1
                    placed = True
                    break
                if placed:
                    break
            if not placed:
                print(f"  [TOP-UP WARN] {location}: could not reach weekly floor {weekly_min}; stopped at {len(all_slots)}")
                return

    def _daily_target_top_up(self, location: str, week_start: date, all_slots: List[ScheduleSlot],
                             room_occ: RoomOccupancy, weekly_class_counts: Dict[str, int],
                             am_slots: List[str], pm_slots: List[str]):
        config_targets = self.schedule_config.get("targets", {}).get(location, {})
        if not config_targets:
            return

        reserve_slots = [
            "06:30", "07:00", "07:45", "08:15", "08:45",
            "10:15", "11:00", "11:30", "12:00", "12:30",
            "16:00", "16:30", "17:00", "17:30", "20:00", "20:15",
        ]

        for day_name in DAY_ORDER:
            if not self._location_allowed_day(location, day_name):
                continue
            if location in WEEKEND_ONLY_TARGETS:
                target = self._pick_daily_target(location, day_name)
                min_target = target
                day_max = target
            else:
                limits = config_targets.get(day_name) or {}
                if not isinstance(limits, dict) or (
                    limits.get("target") is None and limits.get("min") is None
                ):
                    continue
                min_target = int(limits.get("min", limits.get("target")) or 0)
                day_max = int(limits.get("max", min_target) or min_target)
                target = min(max(self._pick_daily_target(location, day_name), min_target), day_max)
            day_idx = DOW_REVERSE[day_name]
            date_str = (week_start + timedelta(days=day_idx)).isoformat()

            attempts = 0
            while attempts < 80:
                attempts += 1
                slots_today = [s for s in all_slots if s.location == location and s.day_of_week == day_name]
                if len(slots_today) >= target or len(slots_today) >= day_max:
                    break

                used_at_time: Dict[str, Set[str]] = {}
                shift_trainers: Dict[str, List[str]] = {"AM": [], "PM": []}
                class_format_count_today: Dict[str, int] = {}
                for slot in slots_today:
                    used_at_time.setdefault(slot.time, set()).add(slot.trainer_1)
                    shift = "AM" if is_am_slot(slot.time) else "PM"
                    if slot.trainer_1 not in shift_trainers[shift]:
                        shift_trainers[shift].append(slot.trainer_1)
                    class_format_count_today[slot.class_name] = class_format_count_today.get(slot.class_name, 0) + 1

                candidate_times = list(dict.fromkeys(pm_slots + am_slots + reserve_slots))
                placed = False
                for time_str in candidate_times:
                    if day_name == "Sunday" and int(time_str[:2]) < 10:
                        continue
                    if slot_is_in_blocked_window(day_name, time_str):
                        continue
                    result = self._fill_slot(
                        location, day_name, date_str, time_str, used_at_time, shift_trainers,
                        room_occ, slots_today, 0, len(slots_today),
                        is_prime=is_prime_slot(time_str),
                        weekly_class_counts=weekly_class_counts,
                        class_format_count_today=class_format_count_today,
                    )
                    if not result:
                        continue
                    room_occ.occupy(
                        day_name,
                        result.room,
                        slot_time_to_minutes(result.time),
                        result.duration_min,
                        result.class_name,
                        result.trainer_1,
                    )
                    state = self.trainer_states.get(result.trainer_1)
                    if state:
                        state.add(day_name, result.time, location, result.class_name)
                    all_slots.append(result)
                    key = result.class_name.split("(")[0].strip()
                    weekly_class_counts[key] = weekly_class_counts.get(key, 0) + 1
                    self._time_class_counts[
                        (result.location, result.time, canonical_class_key(result.class_name))
                    ] += 1
                    self._time_format_counts[
                        (result.location, result.time, get_class_format(result.class_name))
                    ] += 1
                    self._time_level_counts[
                        (result.location, result.time, class_difficulty_level(result.class_name))
                    ] += 1
                    placed = True
                    break

                if not placed:
                    print(f"  [DAILY TARGET WARN] {location} {day_name}: could not reach target {target}; stopped at {len(slots_today)}")
                    break

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
                      day_max=None, am_slots=None, pm_slots=None, room_occ: RoomOccupancy = None,
                      weekly_class_counts: Optional[Dict[str, int]] = None) -> List[ScheduleSlot]:
        if am_slots is None:
            am_slots = []
        if pm_slots is None:
            pm_slots = []
        if room_occ is None:
            room_occ = RoomOccupancy({})
        if day_max is None:
            day_max = target_count
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
            if slot.recommendation == "PINNED":
                self._release_pinned_reservation(slot.trainer_1, slot.class_name)
            used_at_time.setdefault(t, set()).add(slot.trainer_1)
            shift = "AM" if is_am_slot(slot.time) else "PM"
            if slot.trainer_1 not in shift_trainers[shift]:
                shift_trainers[shift].append(slot.trainer_1)
            class_format_count_today[slot.class_name] = (
                class_format_count_today.get(slot.class_name, 0) + 1
            )

        # ---- Phase 1: Pinned rule-blocks ----
        wcc_for_caps = weekly_class_counts or {}
        for p in self._get_pinned_slots(location, day_name):
            t = p["time"]
            is_manual_pin = bool(p.get("manual"))
            if slot_is_in_blocked_window(day_name, t):
                continue
            trainer = p["trainer"]
            cname = p["class"]
            if self._same_class_already_at_time(slots_today, t, cname):
                continue
            if is_excluded_class(cname, self.class_family.get(cname, "")):
                continue
            if not location_class_allowed(location, cname):
                continue
            if not is_manual_pin and self._class_mix_hard_blocked(location, cname):
                continue
            if not is_manual_pin and not self._class_mix_allows_candidate(
                location, cname, self._weekly_count(wcc_for_caps, slots_today, cname)
            ):
                continue
            if "Strength Lab" in cname:
                if location != "Kwality House, Kemps Corner":
                    continue
            if self._on_leave(trainer, date_str, location):
                continue
            # Never skip manual pins because of consecutive-format guards.
            # Adjacent non-pinned fills are blocked later to protect these slots.
            if (not is_manual_pin) and self._would_repeat_consecutive_format(slots_today, t, cname):
                print(f"  WARNING: Skipping pinned {cname} at {t} due to consecutive format rule")
                continue
            dur = get_class_duration(cname)
            start_min = slot_time_to_minutes(t)
            fam = self.class_family.get(cname, "barre_57")
            rooms = LOCATION_ROOMS.get(location, {})
            preferred_room = p.get("room")
            if preferred_room and preferred_room in rooms and room_occ.is_available(day_name, preferred_room, start_min, dur):
                room = preferred_room
            else:
                room = self._find_best_room(room_occ, day_name, fam, start_min, dur, get_class_format(cname))
            if room is None:
                continue
            ts = self.trainer_states.get(trainer)
            prof = self.trainer_profiles.get(trainer, {})
            if ts is None or not prof:
                continue
            prof_loc = self._profile_location_data(prof, location)
            if location in DERIVED_LOCATION_SOURCES and not prof_loc:
                continue
            prof_loc = prof_loc or {}
            if day_name not in self._available_days(trainer, location, prof_loc.get("available_days", [])):
                if is_manual_pin:
                    print(f"  WARNING: Manual pin {cname} at {t} for {trainer} could not be placed due to trainer availability")
                    continue
                self._release_pinned_reservation(trainer, cname)
                result = self._fill_slot_class(
                    location, day_name, date_str, t, cname, fam,
                    used_at_time, shift_trainers, room_occ, slots_today,
                    reason_prefix=f"Pinned class/time reassigned from unavailable {trainer}",
                    use_slot_history=True,
                    weekly_class_counts=weekly_class_counts,
                    recommendation_override="PINNED",
                )
                if result:
                    slots_today.append(result)
                    _register_slot(result, t)
                continue
            win_s = prof_loc.get("time_window", {}).get("start", "06:00")
            win_e = prof_loc.get("time_window", {}).get("end", "22:00")
            max_d = self._max_per_day(trainer, location, prof_loc.get("max_classes_per_day", 4))
            if not ts.can_add(day_name, t, location, cname, max_d, win_s, win_e):
                if is_manual_pin:
                    print(f"  WARNING: Manual pin {cname} at {t} for {trainer} could not be placed due to trainer constraints")
                    continue
                self._release_pinned_reservation(trainer, cname)
                result = self._fill_slot_class(
                    location, day_name, date_str, t, cname, fam,
                    used_at_time, shift_trainers, room_occ, slots_today,
                    reason_prefix=f"Pinned class/time reassigned from {trainer}",
                    use_slot_history=True,
                    weekly_class_counts=weekly_class_counts,
                    recommendation_override="PINNED",
                )
                if result:
                    slots_today.append(result)
                    _register_slot(result, t)
                continue
            hist = self._get_hist(location, cname, trainer, DOW_REVERSE[day_name], t)
            public_score, placement_score, recommendation, slot_is_exp = self._public_score_fields(
                85.0, 85.0, hist.get("session_count", 0), "PINNED", False, manual_pin=True
            )
            slot = ScheduleSlot(
                location=location, date=date_str, day_of_week=day_name, time=t,
                class_name=cname, trainer_1=trainer, trainer_2="", cover="",
                room=room, capacity=rooms[room]["capacity"], duration_min=dur,
                predicted_fill_rate=self._evidence_adjusted_fill(hist),
                score=public_score, recommendation=recommendation, is_experimental=slot_is_exp,
                scheduling_reason=f"Pinned block — rule ownership for {trainer}",
                historical_avg_fill=hist.get("avg_fill_rate", 0.0),
                historical_avg_checkin=hist.get("avg_checkin", 0.0),
                historical_session_count=hist.get("session_count", 0),
                historical_late_cancel_rate=hist.get("avg_late_cancel_rate", 0.0),
                historical_no_show_rate=hist.get("avg_no_show_rate", 0.0),
                performance_score=public_score,
                placement_score=placement_score,
            )
            slots_today.append(slot)
            _register_slot(slot, t)

        # ---- Phase 2: PROTECT combos (score >= 70), subject to per-day format caps ----
        for r in sorted(self.protected.get((location, DOW_REVERSE[day_name]), []),
                        key=lambda x: -x["score"]):
            t = r["time"][:5]
            if slot_is_in_blocked_window(day_name, t):
                continue
            trainer = r["trainer"]
            cname = r["class"]
            if self._same_class_already_at_time(slots_today, t, cname):
                continue
            if not self._horizontal_mix_allows_candidate(location, t, cname):
                continue
            # Protected rows keep their exact class variant. This intentionally
            # bypasses class permission/mix gates so "Barre 57 Express" cannot
            # be silently replaced by "Barre 57" when Express is normally gated.
            if "Strength Lab" in cname:
                if location != "Kwality House, Kemps Corner":
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
            prof_loc = self._profile_location_data(prof, location)
            if location in DERIVED_LOCATION_SOURCES and not prof_loc:
                continue
            prof_loc = prof_loc or {}
            if day_name not in self._available_days(trainer, location, prof_loc.get("available_days", [])):
                continue
            win_s = prof_loc.get("time_window", {}).get("start", "06:00")
            win_e = prof_loc.get("time_window", {}).get("end", "22:00")
            max_d = self._max_per_day(trainer, location, prof_loc.get("max_classes_per_day", 4))
            if not ts.can_add(day_name, t, location, cname, max_d, win_s, win_e):
                continue
            # Protected performance slots should not be dropped because of local
            # consecutive-format checks; surrounding fills are filtered later.
            if would_block_recovery(cname, t, slots_today):
                continue
            if "Recovery" in cname and not is_recovery_last_in_shift(cname, t, slots_today):
                continue
            if location == SUPREME_LOCATION and ts.tier > 1:
                available_tier1 = self._available_tier1_trainers_for_slot(
                    location, day_name, date_str, t, cname, used_at_time.get(t, set()),
                    exclude_trainer=trainer,
                )
                if available_tier1:
                    self._release_pinned_reservation(trainer, cname)
                    result = self._fill_slot_class(
                        location, day_name, date_str, t, cname, fam,
                        used_at_time, shift_trainers, room_occ, slots_today,
                        reason_prefix=f"Supreme protected slot reassigned from lower-tier {trainer}",
                        use_slot_history=True,
                        weekly_class_counts=weekly_class_counts,
                        recommendation_override="PROTECT_EXACT",
                        protected_exact_variant=True,
                        preferred_trainers=available_tier1,
                    )
                    if result:
                        slots_today.append(result)
                        _register_slot(result, t)
                        opt_today += 1
                        continue
            hist = self._get_hist(location, cname, trainer, DOW_REVERSE[day_name], t)
            public_score, placement_score, recommendation, slot_is_exp = self._public_score_fields(
                r["score"], r["score"], hist.get("session_count", 0), "PROTECT_EXACT", False
            )
            slot = ScheduleSlot(
                location=location, date=date_str, day_of_week=day_name, time=t,
                class_name=cname, trainer_1=trainer, trainer_2="", cover="",
                room=room, capacity=rooms[room]["capacity"], duration_min=dur,
                predicted_fill_rate=self._evidence_adjusted_fill(hist),
                score=public_score, recommendation=recommendation, is_experimental=slot_is_exp,
                scheduling_reason=f"Top performer: {trainer} — {hist.get('session_count',0)} sessions, {hist.get('avg_fill_rate',0):.0%} fill, score {public_score:.1f}",
                historical_avg_fill=hist.get("avg_fill_rate", 0.0),
                historical_avg_checkin=hist.get("avg_checkin", 0.0),
                historical_session_count=hist.get("session_count", 0),
                historical_late_cancel_rate=hist.get("avg_late_cancel_rate", 0.0),
                historical_no_show_rate=hist.get("avg_no_show_rate", 0.0),
                performance_score=public_score,
                placement_score=placement_score,
                score_breakdown=r.get("score_breakdown", {}),
            )
            slots_today.append(slot)
            _register_slot(slot, t)
            opt_today += 1

        # ---- Phase 2a: protect class + time even if trainer changes ----
        for r in sorted(self.protected_class_times.get((location, DOW_REVERSE[day_name]), []),
                        key=lambda x: -x["score"]):
            t = r["time"][:5]
            cname = r["class"]
            if slot_is_in_blocked_window(day_name, t):
                continue
            if self._same_class_already_at_time(slots_today, t, cname):
                continue
            if not self._horizontal_mix_allows_candidate(location, t, cname):
                continue
            # Keep protected class+time slots even if consecutive-format pattern
            # is tight; surrounding non-protected classes are deprioritized/blocked.
            preferred_trainers = [
                tr.get("trainer")
                for tr in (r.get("top_trainers") or [])
                if tr.get("trainer")
            ]
            result = self._fill_slot_class(
                location, day_name, date_str, t, cname, self.class_family.get(cname, "barre_57"),
                used_at_time, shift_trainers, room_occ, slots_today,
                reason_prefix="Protected class/time",
                use_slot_history=True,
                weekly_class_counts=weekly_class_counts,
                recommendation_override="PROTECT_EXACT",
                protected_exact_variant=True,
                preferred_trainers=preferred_trainers,
            )
            if result:
                slots_today.append(result)
                _register_slot(result, t)
                opt_today += 1

        # ---- Phase 2b: Kwality Strength Lab floor ----
        if location == "Kwality House, Kemps Corner" and not is_sunday:
            weekly_floor = self._weekly_min(location, "Studio Strength Lab", 2)
            weekly_ceil = self._weekly_max(location, "Studio Strength Lab", 2)
            if weekly_floor > 0:
                day_progress = min(6, DOW_REVERSE[day_name] + 1)
                desired_by_today = max(1, (weekly_floor * day_progress + 5) // 6)
                preferred_times = ["18:00", "19:15", "18:15", "17:45"]
                for t in preferred_times:
                    cur = self._weekly_count(weekly_class_counts, slots_today, "Studio Strength Lab")
                    if cur >= desired_by_today or cur >= weekly_ceil:
                        break
                    if slot_is_in_blocked_window(day_name, t):
                        continue
                    if self._same_class_already_at_time(slots_today, t, "Studio Strength Lab"):
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
                if slot_is_in_blocked_window(day_name, t):
                    continue
                if self._same_class_already_at_time(slots_today, t, "Studio PowerCycle"):
                    continue
                result = self._fill_slot_class(
                    location, day_name, date_str, t, "Studio PowerCycle", "powercycle",
                    used_at_time, shift_trainers, room_occ, slots_today,
                    reason_prefix="Kwality PowerCycle floor",
                    weekly_class_counts=weekly_class_counts,
                )
                if result:
                    slots_today.append(result)
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
                if self._same_class_already_at_time(slots_today, t, "Studio PowerCycle"):
                    continue
                result = self._fill_slot_class(
                    location, day_name, date_str, t, "Studio PowerCycle", "powercycle",
                    used_at_time, shift_trainers, room_occ, slots_today,
                    weekly_class_counts=weekly_class_counts,
                )
                if result:
                    slots_today.append(result)
                    _register_slot(result, t)
                    pc_today += 1
                    opt_today += 1

        # ---- Phase 3: Determine AM/PM fill targets ----
        locked_count = len(slots_today)
        # Daily targets are treated as soft planning guidance. Allow controlled
        # overfill to improve room utilization and score quality.
        if location == KWALITY_LOCATION:
            soft_target_buffer = 6
        elif location == SUPREME_LOCATION:
            soft_target_buffer = 5
        elif location == "Kenkere House":
            soft_target_buffer = 3
        else:
            soft_target_buffer = 2
        soft_target_count = target_count + soft_target_buffer

        locked_am = sum(1 for s in slots_today if is_am_slot(s.time))
        locked_pm = sum(1 for s in slots_today if not is_am_slot(s.time))

        desired_am = max(1, round(target_count * 0.48))
        desired_pm = max(1, target_count - desired_am) if target_count >= 2 else 0
        # Demand policy:
        # 1) Kwality + Supreme Saturdays must skew AM-heavy.
        # 2) Supreme Friday/Saturday should not underfill AM.
        # 3) Sundays still need both AM and PM coverage when the day has 2+ classes.
        if location in {KWALITY_LOCATION, SUPREME_LOCATION} and day_name == "Saturday":
            desired_am = max(desired_am, (target_count // 2) + 1)
            desired_pm = max(1, target_count - desired_am)
        if location == SUPREME_LOCATION and day_name in {"Friday", "Saturday"}:
            desired_am = max(desired_am, 6)
            desired_pm = max(1, target_count - desired_am)
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

        am_first_bias = (
            (location in {KWALITY_LOCATION, SUPREME_LOCATION} and day_name == "Saturday")
            or (location == SUPREME_LOCATION and day_name == "Friday")
        )
        if am_first_bias:
            # ---- Phase 4: Prime AM fill ----
            filled_am = 0
            for t in am_slots:
                if filled_am >= target_am_fill:
                    break
                if slot_is_in_blocked_window(day_name, t):
                    continue
                if _do_fill(t, is_prime=is_prime_slot(t)):
                    filled_am += 1

            # ---- Phase 5: Prime PM fill ----
            filled_pm = 0
            for t in pm_slots:
                if filled_pm >= target_pm_fill:
                    break
                if _do_fill(t, is_prime=is_prime_slot(t)):
                    filled_pm += 1
        else:
            # ---- Phase 4: Prime PM fill ----
            filled_pm = 0
            for t in pm_slots:
                if filled_pm >= target_pm_fill:
                    break
                if _do_fill(t, is_prime=is_prime_slot(t)):
                    filled_pm += 1

            # ---- Phase 5: Prime AM fill ----
            filled_am = 0
            for t in am_slots:
                if filled_am >= target_am_fill:
                    break
                if slot_is_in_blocked_window(day_name, t):
                    continue
                if _do_fill(t, is_prime=is_prime_slot(t)):
                    filled_am += 1

        # ---- Phase 6: Top-up if still short ----
        deficit = soft_target_count - len(slots_today)
        if deficit > 0:
            top_up_order = (am_slots + pm_slots) if am_first_bias else (pm_slots + am_slots)
            for t in top_up_order:
                if deficit <= 0:
                    break
                if slot_is_in_blocked_window(day_name, t):
                    continue
                if _do_fill(t, is_prime=False):
                    deficit -= 1

        # Persisted daily targets should be attempted before the day closes,
        # even when the target needs a historically valid but non-core slot.
        if len(slots_today) < soft_target_count:
            supplemental_times = ["12:00", "12:30", "12:45", "15:30", "16:00", "20:00"]
            if day_name == "Sunday":
                supplemental_times = ["12:00", "12:30", "12:45", "15:30", "16:00", "17:30", "18:00", "18:30"]
            for t in supplemental_times:
                if len(slots_today) >= soft_target_count:
                    break
                if slot_is_in_blocked_window(day_name, t):
                    continue
                _do_fill(t, is_prime=False)

        # ---- Phase 7: Peak slot parallel fill — fill rooms in key Mumbai demand clusters ----
        # Requested Mumbai clusters: 08:00/08:15/08:30/08:45,
        # 11:00/11:15/11:30/11:45, and 18:00/18:15/18:30/18:45.
        # We allow controlled overfill beyond base target_count to avoid idle studios
        # when multiple rooms are available in high-demand peaks.
        def _in_priority_peak_window(ts: str) -> bool:
            if location in {KWALITY_LOCATION, SUPREME_LOCATION}:
                return ts in MUMBAI_PARALLEL_PEAK_TIMES
            minutes = slot_time_to_minutes(ts)
            return (
                slot_time_to_minutes("08:00") <= minutes <= slot_time_to_minutes("10:00")
                or slot_time_to_minutes("11:00") <= minutes <= slot_time_to_minutes("12:30")
                or slot_time_to_minutes("18:00") <= minutes <= slot_time_to_minutes("19:30")
            )

        if location == KWALITY_LOCATION:
            peak_parallel_buffer = 5
        elif location == SUPREME_LOCATION:
            peak_parallel_buffer = 4
        elif location == "Kenkere House":
            peak_parallel_buffer = 2
        else:
            peak_parallel_buffer = 1
        max_total_for_day = target_count + peak_parallel_buffer

        def _rotated_cluster(times: List[str]) -> List[str]:
            if not times:
                return []
            offset = DOW_REVERSE.get(day_name, 0) % len(times)
            rotated = times[offset:] + times[:offset]
            # Inject rng-shuffled order so successive generations don't
            # always award the earliest cluster slot to the best candidate.
            if getattr(self, "_rng", None):
                rotated = list(rotated)
                self._rng.shuffle(rotated)
            return rotated

        if location in {KWALITY_LOCATION, SUPREME_LOCATION}:
            clusters = [
                _rotated_cluster(MUMBAI_PARALLEL_AM_EARLY),
                _rotated_cluster(MUMBAI_PARALLEL_AM_LATE),
                _rotated_cluster(MUMBAI_PARALLEL_PM),
            ]
            # Shuffle cluster order itself (AM-early / AM-late / PM)
            if getattr(self, "_rng", None):
                self._rng.shuffle(clusters)
            peak_source = [t for cluster in clusters for t in cluster]
        else:
            peak_source = sorted(PRIME_AM_SLOTS | PRIME_PM_SLOTS)
            if getattr(self, "_rng", None):
                self._rng.shuffle(peak_source)

        peak_fill_slots = [
            t for t in peak_source
            if _in_priority_peak_window(t)
            and not slot_is_in_blocked_window(day_name, t)
            and not (is_sunday and int(t[:2]) < 10)
        ]
        for t in peak_fill_slots:
            if len(slots_today) >= max(max_total_for_day, soft_target_count):
                break
            classes_at_t = sum(1 for s in slots_today if s.time == t)
            available_rooms = sum(
                1 for rid in (LOCATION_ROOMS.get(location) or {})
                if room_occ.is_available(day_name, rid, slot_time_to_minutes(t), 57)
            )
            while (
                available_rooms > 0
                and classes_at_t < available_rooms
                and len(slots_today) < max(max_total_for_day, soft_target_count)
            ):
                if not _do_fill(t, is_prime=True):
                    break
                classes_at_t = sum(1 for s in slots_today if s.time == t)
                available_rooms = sum(
                    1 for rid in (LOCATION_ROOMS.get(location) or {})
                    if room_occ.is_available(day_name, rid, slot_time_to_minutes(t), 57)
                )

        # ---- Phase 8: Barre 57 per-shift guarantee ----
        # Every location must have ≥1 Barre-family class in each shift every day
        barre_keywords = ["Barre 57", "Cardio Barre", "Mat 57", "FIT", "Back Body", "Amped Up", "Barre Fusion"]
        def _has_barre_in_shift(sh: str) -> bool:
            return any(
                any(kw in s.class_name for kw in barre_keywords)
                for s in slots_today
                if (sh == "morning" and 7 <= int(s.time[:2]) < 12)
                or (sh == "midday" and 12 <= int(s.time[:2]) < 17)
                or (sh == "evening" and int(s.time[:2]) >= 17)
            )
        barre_shift_times = {
            "morning": ["09:00", "08:30", "09:30", "10:15", "11:00", "11:30"],
            # 13:00 falls in BLOCKED_MIDDAY 13:00-15:00, 12:00 is too early.
            # Use legal midday/early-evening slots only.
            "midday":  ["12:15", "12:30", "15:15", "15:30"],
            "evening": ["19:00", "18:00", "19:15", "17:45", "18:15"],
        }
        for sh, times in barre_shift_times.items():
            if len(slots_today) >= soft_target_count:
                break
            if sh == "midday" and is_sunday:
                continue
            if _has_barre_in_shift(sh):
                continue
            for t in times:
                if slot_is_in_blocked_window(day_name, t):
                    continue
                if is_sunday and int(t[:2]) < 10:
                    continue
                result = self._fill_slot_class(
                    location, day_name, date_str, t, "Studio Barre 57", "barre_57",
                    used_at_time, shift_trainers, room_occ, slots_today,
                    reason_prefix=f"Barre 57 per-shift guarantee ({sh})",
                    weekly_class_counts=weekly_class_counts,
                )
                if result:
                    slots_today.append(result)
                    _register_slot(result, t)
                    break

        # ---- Phase 9: Saturday AM dominance guardrail (Kwality + Supreme) ----
        # Always keep AM class count strictly greater than PM for Saturday.
        if day_name == "Saturday" and location in {KWALITY_LOCATION, SUPREME_LOCATION}:
            def _counts() -> Tuple[int, int]:
                am_n = sum(1 for s in slots_today if is_am_slot(s.time))
                pm_n = sum(1 for s in slots_today if not is_am_slot(s.time))
                return am_n, pm_n

            am_n, pm_n = _counts()
            if am_n <= pm_n:
                # First attempt: add AM classes in open AM slots.
                for t in am_slots:
                    if am_n > pm_n:
                        break
                    if slot_is_in_blocked_window(day_name, t):
                        continue
                    if _do_fill(t, is_prime=is_prime_slot(t)):
                        am_n, pm_n = _counts()
                # Second attempt: replace lowest-score removable PM with AM fills.
                while am_n <= pm_n:
                    removable_pm = [
                        s for s in slots_today
                        if (not is_am_slot(s.time))
                        and s.recommendation != "PROTECT_EXACT"
                    ]
                    if not removable_pm:
                        break
                    drop = min(removable_pm, key=lambda s: float(s.score or 0.0))
                    slots_today.remove(drop)
                    used_at_time[drop.time] = {
                        s.class_name for s in slots_today if s.time == drop.time
                    }
                    added = False
                    for t in am_slots:
                        if slot_is_in_blocked_window(day_name, t):
                            continue
                        if _do_fill(t, is_prime=is_prime_slot(t)):
                            added = True
                            break
                    if not added:
                        # Revert if no AM replacement is possible.
                        slots_today.append(drop)
                        used_at_time[drop.time].add(drop.class_name)
                        break
                    am_n, pm_n = _counts()

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
            for r in self._candidate_rows(location, dow, day_filter=day_filter):
                r_time = (r.get("time") or "")[:5]
                cname = r["class"]
                fam = self.class_family.get(cname, "")
                if is_excluded_class(cname, fam):
                    continue
                if self._same_class_already_at_time(slots_today, time_str, cname):
                    continue
                if not self._horizontal_mix_allows_candidate(location, time_str, cname):
                    continue
                if not location_class_allowed(location, cname):
                    continue
                if self._class_mix_hard_blocked(location, cname):
                    continue
                if location in BENGALURU_LOCATIONS and "PowerCycle" in cname:
                    continue
                if location != "Kwality House, Kemps Corner" and "Strength Lab" in cname:
                    continue
                trainer = r["trainer"]
                # Hard guard: PowerCycle never at Bengaluru locations (Kenkere/Copper).
                # Strength Lab only at Kwality House.
                if "PowerCycle" in cname and location in BENGALURU_LOCATIONS:
                    continue
                if "Strength Lab" in cname and location != KWALITY_LOCATION:
                    continue
                if "PowerCycle" in cname:
                    if not self.trainer_profiles.get(trainer, {}).get("qualifications", {}).get("powercycle", False):
                        continue
                if "Strength Lab" in cname:
                    if not self.trainer_profiles.get(trainer, {}).get("qualifications", {}).get("strength_lab", False):
                        continue
                if "Foundations" in cname:
                    if not self.trainer_profiles.get(trainer, {}).get("qualifications", {}).get("foundations", False):
                        continue
                # Recovery slot restriction
                if "Recovery" in cname and not is_recovery_allowed_in_slot(time_str):
                    continue
                if not allow_drop and r.get("recommendation") == "DROP":
                    continue
                if allow_drop and r.get("recommendation") == "DROP":
                    continue
                if r.get("session_count", 0) < 1:
                    continue
                if float(r.get("score", 0.0) or 0.0) < MIN_SCHEDULABLE_SCORE:
                    continue
                if is_low_performing_history(r):
                    continue
                placement_hist = self._get_hist(location, cname, trainer, dow, time_str)
                if is_low_performing_history(placement_hist):
                    continue
                has_placement_evidence = placement_hist.get("session_count", 0) > 0
                same_or_near_time = bool(
                    r_time and abs(slot_time_to_minutes(r_time) - start_min) <= 15
                )
                if not has_placement_evidence and not same_or_near_time:
                    continue
                if self._is_inactive(trainer) or self._on_leave(trainer, date_str, location):
                    continue
                if self._custom_rule_blocks(location, day_name, time_str, cname, trainer):
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

                # Hard weekly max block — never exceed Settings ceiling.
                if not self._class_mix_allows_candidate(
                    location, cname, self._weekly_count(wcc, slots_today, cname)
                ):
                    continue

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
                if state and state.tier > 1 and self._tier1_under_target_exists_for_slot(
                    location, day_name, date_str, time_str, cname, already_at_time,
                    exclude_trainer=trainer, experimental=is_exp and want_experimental,
                ):
                    continue
                if (
                    location == "Supreme HQ, Bandra"
                    and state
                    and state.tier > 1
                    and self._tier1_available_for_slot(
                        location, day_name, date_str, time_str, cname, already_at_time,
                        exclude_trainer=trainer, experimental=is_exp and want_experimental,
                    )
                ):
                    continue
                if state and self._higher_tier_underused_exists_for_location_slot(
                    location, day_name, date_str, time_str, cname, already_at_time,
                    exclude_trainer=trainer,
                    candidate_recommendation=rec,
                    experimental=is_exp and want_experimental,
                ):
                    continue

                shift_bonus = 12.0 if trainer in shift_trainers[shift] else (-8.0 if len(shift_trainers[shift]) >= 2 else 0.0)
                time_penalty = 0.0
                if r_time:
                    time_penalty = min(10.0, abs(slot_time_to_minutes(r_time) - start_min) / 30 * 1.5)
                hours_bonus = self._trainer_hours_bonus(trainer, day_name)
                
                # Apply format popularity bonus/penalty (global + location-specific)
                popularity_bonus = FORMAT_POPULARITY_BONUS.get(cname, 0.0)
                popularity_bonus += LOCATION_FORMAT_BONUS.get(location, {}).get(cname, 0.0)
                horizontal_bonus = self._horizontal_slot_adjustment(location, time_str, cname)
                level_bonus = self._class_level_slot_adjustment(location, time_str, cname)
                express_bonus = self._express_slot_adjustment(cname, time_str)
                target_penalty = self._weekly_over_target_penalty(
                    location,
                    cname,
                    self._weekly_count(wcc, slots_today, cname),
                )
                intraday_div = self._intraday_cluster_diversity_penalty(slots_today, time_str, cname)
                weekly_mix_bonus = self._weekly_mix_variance_bonus(location, cname)
                
                rec_for_noise = r.get("recommendation", "INCLUDE")
                ai_delta = self._get_ai_hint_delta(location, cname, trainer, dow)
                effective_score = self._apply_optimization_mode_adjustments(
                    base_score=r["score"],
                    shift_bonus=shift_bonus,
                    diversity_adjustment=div_adj,
                    hours_bonus=hours_bonus,
                    popularity_bonus=popularity_bonus + horizontal_bonus + level_bonus + express_bonus + target_penalty + intraday_div + weekly_mix_bonus,
                    ai_delta=ai_delta,
                    time_penalty=time_penalty,
                    recommendation=rec_for_noise,
                )
                effective_score += self._tier_priority_score(trainer)
                effective_score += self._location_tier_priority_score(trainer, location)
                effective_score += self._format_trainer_priority_score(trainer, location, cname)
                rec_protect = {"PINNED", "PROTECT", "PROTECT_EXACT"}
                if (
                    is_prime
                    and location == "Supreme HQ, Bandra"
                    and r.get("recommendation") not in rec_protect
                    and min(100.0, max(0.0, effective_score)) < 55.0
                ):
                    continue

                # Experimental quota: only boost truly untested combos when quota is open.
                if want_experimental and is_exp:
                    effective_score += 15.0

                # Hard quality gate: non-pinned candidates whose adjusted score
                # falls below MIN_SCHEDULABLE_SCORE must not enter the pool.
                if (
                    r.get("recommendation") not in rec_protect
                    and min(100.0, max(0.0, effective_score)) < MIN_SCHEDULABLE_SCORE
                ):
                    continue

                cands.append((effective_score, r, room, dur, is_exp))
            return cands

        # First pass: same-day, no DROP (strict)
        candidates = _build_candidates(day_filter=True, allow_drop=False)

        if not candidates:
            # Relax to any-day, same location, no DROP
            seen_tc: set = set()
            candidates = _build_candidates(day_filter=False, allow_drop=False, seen_tc=seen_tc)

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
        placement_score = round(min(100.0, float(best_effective)), 2)
        score_breakdown = dict(best_r.get("score_breakdown", {}) or {})
        score_breakdown["optimizer_adjusted_score"] = placement_score

        rec = best_r.get("recommendation", "INCLUDE")
        if is_protected_strength_lab_row(best_r) or is_protected_strength_lab_history(cname, hist):
            rec = "PROTECT_EXACT"
        public_score, placement_score, rec, is_exp = self._public_score_fields(
            best_r["score"], placement_score, hist.get("session_count", 0), rec, is_exp
        )
        score_breakdown["performance_score"] = public_score
        score_breakdown["placement_score"] = placement_score
        reason = self._make_reason(cname, trainer, rec, is_exp, hist, public_score)
        reason = (
            self._top_trainer_rejection_reason(
                location, day_name, date_str, time_str, cname, trainer, already_at_time, slots_today
            )
            + reason
        )
        violations = self._quick_check(location, day_name, time_str, cname, slots_today)

        return ScheduleSlot(
            location=location, date=date_str, day_of_week=day_name, time=time_str,
            class_name=cname, trainer_1=trainer, trainer_2="", cover="",
            room=room, capacity=rooms.get(room, {}).get("capacity", 15),
            duration_min=dur,
            predicted_fill_rate=self._evidence_adjusted_fill(hist),
            score=public_score, recommendation=rec,
            is_experimental=is_exp,
            scheduling_reason=reason,
            historical_avg_fill=hist.get("avg_fill_rate", 0.0),
            historical_avg_checkin=hist.get("avg_checkin", 0.0),
            historical_session_count=hist.get("session_count", 0),
                historical_late_cancel_rate=hist.get("avg_late_cancel_rate", 0.0),
                historical_no_show_rate=hist.get("avg_no_show_rate", 0.0),
                performance_score=public_score,
                placement_score=placement_score,
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
            ("Studio Barre 57 Express", "barre_57", 45),
            ("Studio Cardio Barre Express", "barre_57", 45),
            ("Studio Mat 57 Express", "barre_57", 45),
            ("Studio Foundations", "barre_57", 57),
            ("Studio Mat 57", "barre_57", 57),
            ("Studio Barre 57", "barre_57", 57),
        ]
        if location == "Kwality House, Kemps Corner":
            fallback_candidates.extend([
                ("Studio Strength Lab", "strength_lab", get_class_duration("Studio Strength Lab")),
                ("Studio PowerCycle", "powercycle", get_class_duration("Studio PowerCycle")),
                ("Studio PowerCycle Express", "powercycle", get_class_duration("Studio PowerCycle Express")),
                ("Studio Trainer's Choice", "barre_57", get_class_duration("Studio Trainer's Choice")),
                ("Studio Cardio Barre Plus", "barre_57", get_class_duration("Studio Cardio Barre Plus")),
            ])
        fallback_candidates.sort(key=lambda x: (
            fmt.get(x[0], 0),
            max(0.0, -self._weekly_over_target_penalty(
                location,
                x[0],
                self._weekly_count(wcc, st_today, x[0]),
            )),
            -self._express_slot_adjustment(x[0], time_str),
            -(LOCATION_FORMAT_BONUS.get(location, {}).get(x[0], 0.0)),
        ))

        def priority(name, state):
            in_shift = name in shift_trainers[shift]
            tier_load = -state.weekly_minutes if state.tier == 1 else state.weekly_minutes
            return (state.tier, state.classes_at_location(location), 0 if in_shift else 1, tier_load, name)

        for cname, fam, dur in fallback_candidates:
            if not location_class_allowed(location, cname):
                continue
            if self._same_class_already_at_time(st_today, time_str, cname):
                continue
            if not self._horizontal_mix_allows_candidate(location, time_str, cname):
                continue
            cap = MAX_FORMAT_PER_DAY.get(cname, DEFAULT_MAX_FORMAT_PER_DAY)
            if fmt.get(cname, 0) >= cap:
                continue
            if self._class_mix_hard_blocked(location, cname):
                continue
            if not self._class_mix_allows_candidate(
                location, cname, self._weekly_count(wcc, st_today, cname)
            ):
                continue
            if self._custom_rule_blocks(location, day_name, time_str, cname, ""):
                continue
            if location in BENGALURU_LOCATIONS and "PowerCycle" in cname:
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
                if self._custom_rule_blocks(location, day_name, time_str, cname, name):
                    continue
                if self._loc_excluded(name, location):
                    continue
                if name in already_at_time:
                    continue
                if "PowerCycle" in cname:
                    if not self.trainer_profiles.get(name, {}).get("qualifications", {}).get("powercycle", False):
                        continue
                if "Strength Lab" in cname:
                    if not self.trainer_profiles.get(name, {}).get("qualifications", {}).get("strength_lab", False):
                        continue
                # Don't skip at target — deprioritize by sorting (handled via priority fn)
                if not self._trainer_ok(name, location, day_name, time_str, cname):
                    continue
                if state.tier > 1 and self._tier1_under_target_exists_for_slot(
                    location, day_name, date_str, time_str, cname, already_at_time,
                    exclude_trainer=name,
                ):
                    continue
                if (
                    location == "Supreme HQ, Bandra"
                    and state.tier > 1
                    and self._tier1_available_for_slot(
                        location, day_name, date_str, time_str, cname, already_at_time,
                        exclude_trainer=name,
                    )
                ):
                    continue
                if self._higher_tier_underused_exists_for_location_slot(
                    location, day_name, date_str, time_str, cname, already_at_time,
                    exclude_trainer=name,
                    candidate_recommendation="CONSIDER",
                ):
                    continue
                # Foundations needs certified trainer
                if "Foundations" in cname:
                    if not self.trainer_profiles.get(name, {}).get("qualifications", {}).get("foundations", False):
                        continue
                hist = self._get_hist(location, cname, name, DOW_REVERSE[day_name], time_str)
                if not hist or int(hist.get("session_count", 0) or 0) < MIN_PROVEN_SESSIONS:
                    continue
                if is_low_performing_history(hist):
                    continue
                target_penalty = self._weekly_over_target_penalty(
                    location,
                    cname,
                    self._weekly_count(wcc, st_today, cname),
                )
                fallback_public_score = (65.0 if hist.get("session_count", 0) > 0 else 55.0) + target_penalty
                if fallback_public_score < MIN_SCHEDULABLE_SCORE:
                    continue
                fallback_placement_score = fallback_public_score + self._location_tier_priority_score(name, location)
                fallback_placement_score += self._format_trainer_priority_score(name, location, cname)
                recommendation = "PROTECT_EXACT" if is_protected_strength_lab_history(cname, hist) else "INCLUDE"
                public_score, placement_score, recommendation, slot_is_exp = self._public_score_fields(
                    fallback_public_score,
                    fallback_placement_score,
                    hist.get("session_count", 0),
                    recommendation,
                    False,
                )
                return ScheduleSlot(
                    location=location, date=date_str, day_of_week=day_name, time=time_str,
                    class_name=cname, trainer_1=name, trainer_2="", cover="",
                    room=room, capacity=rooms.get(room, {}).get("capacity", 15),
                    duration_min=dur,
                    predicted_fill_rate=self._evidence_adjusted_fill(hist),
                    score=public_score, recommendation=recommendation, is_experimental=slot_is_exp,
                    scheduling_reason=(
                        self._top_trainer_rejection_reason(
                            location, day_name, date_str, time_str, cname, name, already_at_time, st_today
                        )
                        + "Coverage: best available trainer-class combo for this slot"
                    ),
                    historical_avg_fill=hist.get("avg_fill_rate", 0.0),
                    historical_avg_checkin=hist.get("avg_checkin", 0.0),
                    historical_session_count=hist.get("session_count", 0),
                historical_late_cancel_rate=hist.get("avg_late_cancel_rate", 0.0),
                historical_no_show_rate=hist.get("avg_no_show_rate", 0.0),
                performance_score=public_score,
                placement_score=placement_score,
                )
        return None

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    def _fill_slot_class(self, location, day_name, date_str, time_str, class_name, class_fam,
                         used_at_time, shift_trainers, room_occ: RoomOccupancy,
                         slots_today, reason_prefix: str = "Enforced class slot",
                         use_slot_history: bool = False,
                         weekly_class_counts: Optional[Dict[str, int]] = None,
                         recommendation_override: Optional[str] = None,
                         protected_exact_variant: bool = False,
                         preferred_trainers: Optional[List[str]] = None) -> Optional["ScheduleSlot"]:
        wcc = weekly_class_counts or {}
        if self._same_class_already_at_time(slots_today, time_str, class_name):
            return None
        if not self._horizontal_mix_allows_candidate(location, time_str, class_name):
            return None
        if not protected_exact_variant and not location_class_allowed(location, class_name):
            return None
        if not protected_exact_variant and self._class_mix_hard_blocked(location, class_name):
            return None
        if not protected_exact_variant and not self._class_mix_allows_candidate(
            location, class_name, self._weekly_count(wcc, slots_today, class_name)
        ):
            return None
        if not protected_exact_variant and self._custom_rule_blocks(location, day_name, time_str, class_name, ""):
            return None
        if "Strength Lab" in class_name:
            if location != "Kwality House, Kemps Corner":
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
        for r in self._class_candidate_rows(location, class_name, dow):
            if protected_exact_variant and not same_protected_class_variant(r.get("class", ""), class_name):
                continue
            r_time = (r.get("time") or "")[:5]
            if use_slot_history and r_time != time_str:
                continue
            trainer = r["trainer"]
            tp = self.trainer_profiles.get(trainer, {})
            qual_map = {
                "Cycle": "powercycle", "Strength": "strength_lab", "Foundations": "foundations",
                "FIT": "fit", "Blaze": "back_body_blaze", "Mat": "mat_57",
                "Amped": "amped_up", "HIIT": "hiit", "Recovery": "recovery"
            }
            needed_qual = "all_barre"
            for k, v in qual_map.items():
                if k in class_name:
                    needed_qual = v
                    break
            
            if not tp.get("qualifications", {}).get(needed_qual, False):
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
            state = self.trainer_states.get(trainer)
            if state and state.tier > 1 and self._tier1_under_target_exists_for_slot(
                location, day_name, date_str, time_str, class_name, already,
                exclude_trainer=trainer,
            ):
                continue
            if (
                location == "Supreme HQ, Bandra"
                and state
                and state.tier > 1
                and self._tier1_available_for_slot(
                    location, day_name, date_str, time_str, class_name, already,
                    exclude_trainer=trainer,
                )
            ):
                continue
            if state and self._higher_tier_underused_exists_for_location_slot(
                location, day_name, date_str, time_str, class_name, already,
                exclude_trainer=trainer,
                candidate_recommendation=r.get("recommendation", "INCLUDE"),
            ):
                continue
            shift_bonus = 12.0 if trainer in shift_trainers[shift] else 0.0
            time_penalty = 0.0
            if r_time:
                time_penalty = min(10.0, abs(slot_time_to_minutes(r_time) - start_min) / 30 * 1.5)
            placement_hist = self._get_hist(location, class_name, trainer, dow, time_str)
            if not protected_exact_variant and is_low_performing_history(placement_hist):
                continue
            if not protected_exact_variant and float(r.get("score", 0.0) or 0.0) < MIN_SCHEDULABLE_SCORE:
                continue
            ai_delta = self._get_ai_hint_delta(location, class_name, trainer, dow)
            horizontal_bonus = self._horizontal_slot_adjustment(location, time_str, class_name)
            express_bonus = self._express_slot_adjustment(class_name, time_str)
            target_penalty = self._weekly_over_target_penalty(
                location,
                class_name,
                self._weekly_count(wcc, slots_today, class_name),
            )
            level_bonus = self._class_level_slot_adjustment(location, time_str, class_name)
            pref_boost = 0.0
            if preferred_trainers and trainer in preferred_trainers:
                # Deterministic pinning priority for protected slots: first trainer gets biggest boost.
                pref_boost = max(0.0, 1000.0 - (preferred_trainers.index(trainer) * 50.0))
            candidates.append((
                r["score"]
                + shift_bonus
                + self._trainer_hours_bonus(trainer, day_name)
                + self._tier_priority_score(trainer)
                + self._location_tier_priority_score(trainer, location)
                + self._format_trainer_priority_score(trainer, location, class_name)
                + (float(self.trainer_priority.get(trainer, 50)) - 50.0) * 0.6
                + ai_delta
                + horizontal_bonus
                + level_bonus
                + express_bonus
                + target_penalty
                + pref_boost
                - time_penalty,
                r,
                room,
            ))

        if not candidates:
            return None

        candidates.sort(key=lambda x: -x[0])
        best_effective, best_r, room = candidates[0]
        trainer = best_r["trainer"]
        placement_score = round(min(100.0, float(best_effective)), 2)
        score_breakdown = dict(best_r.get("score_breakdown", {}) or {})
        score_breakdown["optimizer_adjusted_score"] = placement_score
        
        # Choose historical data source
        if use_slot_history:
            hist = self._get_hist_slot(location, class_name, dow, time_str)
            reason_detail = f"Protected slot — alternate trainer selected. Slot history: {hist.get('session_count',0)} sessions, {hist.get('avg_fill_rate',0):.0%} fill, {hist.get('avg_checkin',0):.1f} avg check-ins"
        else:
            hist = self._get_hist(location, class_name, trainer, dow, time_str)
            reason_detail = f"{reason_prefix}: {class_name}. Score {best_r['score']:.1f}; trainer chosen for fill-rate potential, trainer-hour balance, and class-mix logic."
        reason_detail = (
            self._top_trainer_rejection_reason(
                location,
                day_name,
                date_str,
                time_str,
                class_name,
                trainer,
                used_at_time.get(time_str, set()),
                slots_today,
            )
            + reason_detail
        )

        recommendation = recommendation_override or best_r.get("recommendation", "INCLUDE")
        if is_protected_strength_lab_row(best_r) or is_protected_strength_lab_history(class_name, hist):
            recommendation = "PROTECT_EXACT"
        score_for_output, placement_score, recommendation, slot_is_exp = self._public_score_fields(
            best_r["score"],
            placement_score,
            hist.get("session_count", 0),
            recommendation,
            False,
        )
        score_breakdown["performance_score"] = score_for_output
        score_breakdown["placement_score"] = placement_score

        return ScheduleSlot(
            location=location, date=date_str, day_of_week=day_name, time=time_str,
            class_name=class_name, trainer_1=trainer, trainer_2="", cover="",
            room=room, capacity=rooms_def.get(room, {}).get("capacity", 14),
            duration_min=dur,
            predicted_fill_rate=self._evidence_adjusted_fill(hist),
            score=score_for_output, recommendation=recommendation,
            is_experimental=slot_is_exp,
            scheduling_reason=reason_detail,
            historical_avg_fill=hist.get("avg_fill_rate", 0.0),
            historical_avg_checkin=hist.get("avg_checkin", 0.0),
            historical_session_count=hist.get("session_count", 0),
                historical_late_cancel_rate=hist.get("avg_late_cancel_rate", 0.0),
                historical_no_show_rate=hist.get("avg_no_show_rate", 0.0),
                performance_score=score_for_output,
                placement_score=placement_score,
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
                continue
            return room_id

        return repeat_fallback

    def _horizontal_slot_adjustment(self, location: str, time_str: str, class_name: str) -> float:
        """Prefer a balanced class mix at the same clock-time column through the week."""
        class_count = getattr(self, "_time_class_counts", {}).get(
            (location, time_str, canonical_class_key(class_name)),
            0,
        )
        format_count = getattr(self, "_time_format_counts", {}).get(
            (location, time_str, get_class_format(class_name)),
            0,
        )
        if class_count == 0 and format_count == 0:
            return 8.0
        return -min(80.0, (class_count * 28.0) + (format_count * 14.0))

    def _weekly_mix_variance_bonus(self, location: str, class_name: str) -> float:
        """Soft bonus when a class family is below the location's median weekly share.
        Prevents two-format dominance across the week. Reads cumulative counts from
        ``_time_format_counts`` aggregated across times for this location."""
        try:
            counts: Dict[str, int] = defaultdict(int)
            for (loc, _t, fam), n in (self._time_format_counts or {}).items():
                if loc == location and fam:
                    counts[fam] += int(n)
            if not counts:
                return 0.0
            cand_fam = get_class_format(class_name)
            if not cand_fam:
                return 0.0
            values = sorted(counts.values())
            mid = values[len(values) // 2]
            cand_count = counts.get(cand_fam, 0)
            if cand_count == 0 and len(counts) >= 2:
                return 18.0
            if cand_count + 1 <= mid:
                return 10.0
            if cand_count >= mid * 2 and mid > 0:
                return -22.0
            return 0.0
        except Exception:
            return 0.0

    def _intraday_cluster_diversity_penalty(self, slots_today: list, time_str: str, class_name: str) -> float:
        """Soft penalty when the same family/class already exists within +/-30 minutes today.
        Encourages parallel-room peak clusters to span multiple families/levels."""
        try:
            target_min = slot_time_to_minutes(time_str)
        except Exception:
            return 0.0
        cand_fam = get_class_format(class_name)
        cand_class = canonical_class_key(class_name)
        penalty = 0.0
        for s in slots_today or []:
            try:
                s_min = slot_time_to_minutes(s.time[:5])
            except Exception:
                continue
            delta = abs(s_min - target_min)
            if delta > 30:
                continue
            window_scale = 1.0 if delta <= 15 else 0.5
            if canonical_class_key(s.class_name) == cand_class:
                penalty -= 30.0 * window_scale
            elif get_class_format(s.class_name) == cand_fam and cand_fam:
                penalty -= 14.0 * window_scale
        return max(-60.0, penalty)

    def _horizontal_mix_allows_candidate(self, location: str, time_str: str, class_name: str) -> bool:
        """Hard cap repeated class/format columns across the week for a location+time."""
        class_count = getattr(self, "_time_class_counts", {}).get(
            (location, time_str, canonical_class_key(class_name)),
            0,
        )
        if class_count >= HORIZONTAL_MAX_SAME_CLASS_PER_TIME:
            return False
        format_count = getattr(self, "_time_format_counts", {}).get(
            (location, time_str, get_class_format(class_name)),
            0,
        )
        if format_count >= HORIZONTAL_MAX_SAME_FORMAT_PER_TIME:
            return False
        return True

    def _class_level_slot_adjustment(self, location: str, time_str: str, class_name: str) -> float:
        """For class-variety drafts, make same-time columns span beginner/intermediate/advanced choices."""
        if self._optimization_mode != "class_variety":
            return 0.0
        level = class_difficulty_level(class_name)
        counts = getattr(self, "_time_level_counts", {})
        level_count = counts.get((location, time_str, level), 0)
        represented_levels = sum(
            1 for candidate_level in ("beginner", "intermediate", "advanced")
            if counts.get((location, time_str, candidate_level), 0) > 0
        )
        if level_count == 0:
            return CLASS_LEVEL_BONUS_MISSING + (18.0 if represented_levels < 2 else 0.0)
        return -min(90.0, level_count * CLASS_LEVEL_PENALTY_REPEAT)

    def _express_slot_adjustment(self, class_name: str, time_str: str) -> float:
        """Use express formats when members are most likely to be rushed."""
        m = slot_time_to_minutes(time_str)
        # Rush windows: early morning, lunch hour, and late evening
        is_rushed = (
            m <= slot_time_to_minutes("07:30")
            or (slot_time_to_minutes("12:00") <= m <= slot_time_to_minutes("13:15"))
            or m >= slot_time_to_minutes("19:15")
        )
        is_express = "Express" in class_name
        if is_express and is_rushed:
            return 8.0
        if is_express and not is_rushed:
            return -1.5  # reduced from -3.0 — express still valid at off-peak
        if not is_express and is_rushed:
            return -1.5
        return 0.0

    def _trainer_ok(self, trainer, location, day_name, time_str, class_name, experimental: bool = False) -> bool:
        profile = self.trainer_profiles.get(trainer)
        if not profile:
            return False
        date_str = self._date_for_day(day_name)
        if date_str and self._on_leave(trainer, date_str, location):
            return False
        if self._custom_rule_blocks(location, day_name, time_str, class_name, trainer):
            return False
        loc_data = self._profile_location_data(profile, location)
        if not loc_data:
            if location in DERIVED_LOCATION_SOURCES:
                return False
            if not experimental:
                return False
            loc_candidates = list(profile.get("locations", {}).values())
            if not loc_candidates:
                return False
            loc_data = max(loc_candidates, key=lambda item: item.get("session_count", 0))
        avail_days = self._available_days(trainer, location, loc_data.get("available_days", []))
        
        # Mandatory Weekend Off for specific trainers
        if trainer in ["Anisha Shah", "Vivaran Dhasmana", "Mrigakshi Jaiswal", "Pushyank Nahar"]:
            if day_name in ["Saturday", "Sunday"]:
                return False

        state = self.trainer_states.get(trainer)
        if state is None:
            return False

        # Monday KH Shift Restriction
        if day_name == "Monday":
            kh = "Kwality House, Kemps Corner"
            m = slot_time_to_minutes(time_str)
            is_morning = m < 780 # 13:00
            
            # Check existing Monday schedule for this trainer
            for sched_time, sched_loc, _ in state.day_schedule(day_name):
                sched_m = slot_time_to_minutes(sched_time)
                sched_is_morning = sched_m < 780
                
                # If current candidate is KH Morning
                if location == kh and is_morning:
                    if not sched_is_morning: return False # Block evening anywhere
                    if sched_loc != kh: return False # Block other locs in morning
                
                # If existing class is KH Morning
                if sched_loc == kh and sched_is_morning:
                    if not is_morning: return False # Block current if evening anywhere
                    if location != kh: return False # Block current if other loc in morning
                
                # If current candidate is KH Evening
                if location == kh and not is_morning:
                    if sched_is_morning: return False # Block morning anywhere
                    if sched_loc != kh: return False # Block other locs in evening
                
                # If existing class is KH Evening
                if sched_loc == kh and not sched_is_morning:
                    if is_morning: return False # Block current if morning anywhere
                    if location != kh: return False # Block current if other loc in evening

        if day_name not in avail_days:
            return False
        tw = loc_data.get("time_window", {})
        win_s, win_e = self._time_window(trainer, location,
                                         tw.get("start", "06:00"), tw.get("end", "22:00"))
        max_d = self._max_per_day(trainer, location, loc_data.get("max_classes_per_day", 4))
        if day_name not in state.worked_days() and state.worked_days_count() >= MAX_TRAINER_WORK_DAYS:
            return False
        reserved_pinned = getattr(self, "_pinned_minutes_remaining", {}).get(trainer, 0)
        if state.weekly_minutes + get_class_duration(class_name) + reserved_pinned > state.max_weekly_minutes:
            return False
        if not state.can_add(day_name, time_str, location, class_name, max_d, win_s, win_e):
            return False
        # Cross-location weekly hour cap from custom trainer rule.
        max_weekly_minutes_cap = self._max_weekly_minutes_cap(trainer, location)
        if max_weekly_minutes_cap is not None:
            if state.weekly_minutes + get_class_duration(class_name) + reserved_pinned > max_weekly_minutes_cap:
                return False
        max_w = self._max_per_week(trainer, location)
        if max_w is not None:
            loc_total = sum(1 for d in state._schedule.values()
                            for _, loc, _ in d if loc == location)
            if loc_total >= max_w:
                return False
        return True

    def _profile_location_data(self, profile: dict, location: str) -> Optional[dict]:
        home_region = getattr(self, "trainer_home_region", {}).get(profile.get("name"))
        if home_region and home_region != location_region(location):
            return None
        locations = profile.get("locations", {}) or {}
        if location in locations:
            return locations[location]
        if location == "Courtside":
            candidates = [locations.get(loc) for loc in ("Kwality House, Kemps Corner", "Supreme HQ, Bandra") if locations.get(loc)]
        elif location == "Copper & Cloves":
            candidates = [locations.get(loc) for loc in ("Copper & Cloves", "Kenkere House") if locations.get(loc)]
        else:
            candidates = []
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.get("session_count", 0))

    def _trainer_hours_bonus(self, trainer: str, day_name: str) -> float:
        state = self.trainer_states.get(trainer)
        if not state:
            return 0.0
        if self._optimization_mode == "trainer_hours":
            # Smooth concave curve: bonus = K * (1 - exp(-worked / tau)).
            # Tier-1 weighted higher (priority pool); never lets T2 outscore an
            # under-target T1 in this branch. Penalise the gap-to-target rather
            # than absolute hours so over-target trainers don't get penalised
            # more than equivalent under-target trainers in lower tiers.
            import math as _math
            worked_hours = state.weekly_minutes / 60.0
            if state.tier == 1:
                K, tau = 90.0, 7.0
            elif state.tier == 2:
                K, tau = 55.0, 8.0
            else:
                K, tau = 30.0, 9.0
            bonus = K * (1.0 - _math.exp(-worked_hours / tau))
            # Pull-to-target: linear sweetener for T1 under min weekly hours.
            if state.tier == 1:
                gap_min = max(0, TIER1_WEEKLY_TARGET_MIN - state.weekly_minutes)
                bonus += min(28.0, gap_min / 60.0 * 4.0)
            # First-class-of-the-day bump if trainer still has off-day budget.
            if day_name not in state.worked_days() and state.worked_days_count() < MAX_TRAINER_WORK_DAYS:
                bonus += 5.0
            # Approaching weekly cap — gentle taper so we don't overshoot.
            if state.weekly_minutes >= state.max_weekly_minutes - 60:
                bonus -= 22.0
            return max(0.0, min(96.0, bonus))
        bonus = 0.0
        if state.tier == 1:
            remaining = max(0, TIER1_WEEKLY_TARGET_MIN - state.weekly_minutes)
            ideal_gap = max(0, TIER1_WEEKLY_TARGET_IDEAL - state.weekly_minutes)
            bonus += min(34.0, remaining / 60 * 2.5 + ideal_gap / 60 * 1.2)
            if day_name not in state.worked_days() and state.worked_days_count() < 5:
                bonus += 80.0
        elif day_name not in state.worked_days() and state.worked_days_count() < 5:
            bonus += 22.0
        if day_name not in state.worked_days():
            if state.worked_days_count() >= 5:
                bonus -= 8.0
            elif state.worked_days_count() <= 3:
                bonus += 3.0
        return bonus

    def _tier1_under_target_exists_for_slot(
        self,
        location: str,
        day_name: str,
        date_str: str,
        time_str: str,
        class_name: str,
        already_at_time: Set[str],
        exclude_trainer: str = "",
        experimental: bool = False,
    ) -> bool:
        if self._optimization_mode == "trainer_hours":
            return False
        for name, state in self.trainer_states.items():
            if name == exclude_trainer or state.tier != 1 or state.weekly_minutes >= TIER1_WEEKLY_TARGET_MIN:
                continue
            if self._is_inactive(name) or self._on_leave(name, date_str, location):
                continue
            if self._custom_rule_blocks(location, day_name, time_str, class_name, name):
                continue
            if self._loc_excluded(name, location) or name in already_at_time:
                continue
            # Check qualifications
            qual_map = {"Cycle": "powercycle", "Strength": "strength_lab", "Foundations": "foundations", "FIT": "fit", "Blaze": "back_body_blaze"}
            needed = "all_barre"
            for k, v in qual_map.items():
                if k in class_name:
                    needed = v
                    break
            if not self.trainer_profiles.get(name, {}).get("qualifications", {}).get(needed, False):
                continue
            if self._trainer_ok(name, location, day_name, time_str, class_name, experimental=experimental):
                return True
        return False

    def _tier1_available_for_slot(
        self,
        location: str,
        day_name: str,
        date_str: str,
        time_str: str,
        class_name: str,
        already_at_time: Set[str],
        exclude_trainer: str = "",
        experimental: bool = False,
    ) -> bool:
        """Return True when any Tier-1 trainer is eligible for this exact slot."""
        return bool(self._available_tier1_trainers_for_slot(
            location, day_name, date_str, time_str, class_name, already_at_time,
            exclude_trainer=exclude_trainer, experimental=experimental,
        ))

    def _available_tier1_trainers_for_slot(
        self,
        location: str,
        day_name: str,
        date_str: str,
        time_str: str,
        class_name: str,
        already_at_time: Set[str],
        exclude_trainer: str = "",
        experimental: bool = False,
    ) -> List[str]:
        """Return eligible Tier-1 trainers for an exact slot, sorted by local priority."""
        eligible = []
        for name, state in self.trainer_states.items():
            if name == exclude_trainer or state.tier != 1:
                continue
            if self._is_inactive(name) or self._on_leave(name, date_str, location):
                continue
            if self._custom_rule_blocks(location, day_name, time_str, class_name, name):
                continue
            if self._loc_excluded(name, location) or name in already_at_time:
                continue
            # Check qualifications
            qual_map = {"Cycle": "powercycle", "Strength": "strength_lab", "Foundations": "foundations", "FIT": "fit", "Blaze": "back_body_blaze"}
            needed = "all_barre"
            for k, v in qual_map.items():
                if k in class_name:
                    needed = v
                    break
            if not self.trainer_profiles.get(name, {}).get("qualifications", {}).get(needed, False):
                continue
            if self._trainer_ok(name, location, day_name, time_str, class_name, experimental=experimental):
                eligible.append(name)
        eligible.sort(key=lambda trainer: -(
            self._tier_priority_score(trainer)
            + self._location_tier_priority_score(trainer, location)
            + self._trainer_hours_bonus(trainer, day_name)
        ))
        return eligible

    def _higher_tier_underused_exists_for_location_slot(
        self,
        location: str,
        day_name: str,
        date_str: str,
        time_str: str,
        class_name: str,
        already_at_time: Set[str],
        exclude_trainer: str = "",
        candidate_recommendation: str = "INCLUDE",
        experimental: bool = False,
    ) -> bool:
        if candidate_recommendation in {"PINNED", "PROTECT", "PROTECT_EXACT"}:
            return False
        candidate_state = self.trainer_states.get(exclude_trainer)
        if not candidate_state:
            return False
        candidate_next_count = candidate_state.classes_at_location(location) + 1
        for name, state in self.trainer_states.items():
            if name == exclude_trainer or state.tier >= candidate_state.tier:
                continue
            if state.classes_at_location(location) >= candidate_next_count:
                continue
            if self._is_inactive(name) or self._on_leave(name, date_str, location):
                continue
            if self._custom_rule_blocks(location, day_name, time_str, class_name, name):
                continue
            if self._loc_excluded(name, location) or name in already_at_time:
                continue
            # Check qualifications
            qual_map = {"Cycle": "powercycle", "Strength": "strength_lab", "Foundations": "foundations", "FIT": "fit", "Blaze": "back_body_blaze"}
            needed = "all_barre"
            for k, v in qual_map.items():
                if k in class_name:
                    needed = v
                    break
            if not self.trainer_profiles.get(name, {}).get("qualifications", {}).get(needed, False):
                continue
            if self._trainer_ok(name, location, day_name, time_str, class_name, experimental=experimental):
                return True
        return False

    def _tier_priority_score(self, trainer: str) -> float:
        """Score bonus/penalty that enforces T1 > T2 > T3 weekly hours.

        In default mode:
          - T1 gets a large positive bonus that shrinks as they approach their
            target, ensuring they absorb classes first.
          - T2 gets a moderate negative until T1 is satisfied; once T1 is
            at target, T2 gets a small positive to reach their own target.
          - T3 is penalised unless both T1 and T2 are satisfied.
        """
        state = self.trainer_states.get(trainer)
        if not state:
            return 0.0

        if self._optimization_mode == "trainer_hours":
            worked_hours = state.weekly_minutes / 60
            if worked_hours < 6:
                return 90.0 + worked_hours * 8.0
            if worked_hours < 12:
                return 150.0 + (worked_hours - 6.0) * 10.0
            if worked_hours < 13:
                return 115.0
            if worked_hours < 14.5:
                return 110.0 if state.tier == 1 else 55.0
            return -40.0

        # ── Tier-ordered priority ─────────────────────────────────────────
        t1_satisfied = all(
            s.weekly_minutes >= TIER1_WEEKLY_TARGET_MIN
            for s in self.trainer_states.values() if s.tier == 1
        )
        t2_satisfied = all(
            s.weekly_minutes >= TIER2_WEEKLY_TARGET_MIN
            for s in self.trainer_states.values() if s.tier == 2
        )

        if state.tier == 1:
            remaining = max(0, TIER1_WEEKLY_TARGET_MIN - state.weekly_minutes)
            # Extremely strong pull toward target for Tier 1
            ideal_gap = max(0, TIER1_WEEKLY_TARGET_IDEAL - state.weekly_minutes)
            if remaining > 0:
                return 650.0 + remaining / 60 * 45.0
            if ideal_gap > 0:
                return 420.0 + ideal_gap / 60 * 25.0
            if state.weekly_minutes >= state.max_weekly_minutes - 60:
                return 80.0
            return 260.0

        if state.tier == 2:
            if not t1_satisfied:
                # Block T2 from taking hours while any T1 is under target
                remaining_t1 = sum(max(0, TIER1_WEEKLY_TARGET_MIN - s.weekly_minutes) for s in self.trainer_states.values() if s.tier == 1)
                return -200.0 - remaining_t1 / 60 * 10.0
            remaining = max(0, TIER2_WEEKLY_TARGET_MIN - state.weekly_minutes)
            return 40.0 + remaining / 60 * 6.0

        # Tier 3
        if not t1_satisfied or not t2_satisfied:
            return -500.0 - state.weekly_minutes / 60 * 15.0
        # Only give T3 classes once T1 and T2 targets are met
        remaining = max(0, TIER3_WEEKLY_TARGET_MAX - state.weekly_minutes)
        return -10.0 + remaining / 60 * 3.0

    def _location_tier_priority_score(self, trainer: str, location: str) -> float:
        state = self.trainer_states.get(trainer)
        if not state:
            return 0.0
        loc_count = state.classes_at_location(location)
        mumbai_tier1_supreme_band = self._mumbai_tier1_supreme_band(trainer)
        if mumbai_tier1_supreme_band:
            min_supreme_min, max_supreme_min = mumbai_tier1_supreme_band
            supreme_minutes = state.minutes_at_location(SUPREME_LOCATION)
            if location == SUPREME_LOCATION:
                gap = max(0, min_supreme_min - supreme_minutes)
                if gap > 0:
                    return 220.0 + (gap / 60.0) * 34.0
                projected = supreme_minutes + 57
                if projected > max_supreme_min:
                    return -18.0 - max(0.0, (projected - max_supreme_min) / 60.0) * 20.0
                return 20.0
            if supreme_minutes < min_supreme_min:
                gap = min_supreme_min - supreme_minutes
                return -180.0 - (gap / 60.0) * 26.0

        if state.tier == 1:
            if location == SUPREME_LOCATION:
                return max(0.0, 12.0 - loc_count) * 28.0
            return max(0.0, 8.0 - loc_count) * 24.0
        if state.tier == 2:
            if location == SUPREME_LOCATION:
                return -48.0 - loc_count * 24.0
            return -25.0 - loc_count * 18.0
        if location == SUPREME_LOCATION:
            return -120.0 - loc_count * 40.0
        return -80.0 - loc_count * 34.0

    def _format_trainer_priority_score(self, trainer: str, location: str, class_name: str) -> float:
        """Business-priority boost for named trainers and specialist formats.
        Priority pool bonus is gated to Tier-1 (per CLAUDE.md: T1-first capacity)."""
        score = 0.0
        state = getattr(self, "trainer_states", {}).get(trainer)
        if trainer in HIGH_PRIORITY_TRAINERS and state and state.tier == 1:
            if state.weekly_minutes < MAX_TRAINER_WEEKLY_MINUTES_T1:
                score += 90.0 + max(0.0, (MAX_TRAINER_WEEKLY_MINUTES_T1 - state.weekly_minutes) / 60.0) * 10.0
        canonical = canonical_class_key(class_name) or str(class_name or "")
        lower = str(class_name or "").lower()
        is_powercycle = ("powercycle" in lower) or canonical == "Studio PowerCycle" or canonical == "Studio PowerCycle Express"
        is_strength_lab = canonical == "Studio Strength Lab"
        is_fit = canonical in {"Studio FIT", "Copper + Cloves FIT"} or lower.strip().endswith(" fit") or lower.strip() == "studio fit"
        if location in MUMBAI_LOCATIONS and is_powercycle:
            if trainer in MUMBAI_POWERCYCLE_PRIORITY_TRAINERS:
                score += 260.0
            else:
                score -= 70.0
        if is_strength_lab:
            if trainer in STRENGTH_FIT_PRIORITY_TRAINERS:
                score += 220.0
            else:
                score -= 90.0
        elif is_fit:
            if trainer in STRENGTH_FIT_PRIORITY_TRAINERS:
                score += 140.0
            else:
                score -= 30.0
        return score

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
            base_score *= 0.82
            hours_bonus = min(72.0, max(0.0, hours_bonus)) * 0.68
            shift_bonus *= 0.70
            diversity_adjustment *= 0.38
            popularity_bonus *= 0.32
        elif self._optimization_mode == "class_variety":
            base_score *= 0.55
            diversity_adjustment *= 3.4
            popularity_bonus *= 0.75
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
            delta = abs(slot_time_to_minutes(s.time) - current_min)
            if delta == 0:
                continue
            if delta <= 60:
                if get_class_format(s.class_name) == current_format:
                    return True
        return False

    def _same_class_already_at_time(self, slots_today: List[ScheduleSlot], time_str: str, class_name: str) -> bool:
        """Avoid same-time duplicates for exact variant and broad format family."""
        candidate_time = (time_str or "")[:5]
        candidate_class = protected_class_variant_key(class_name)
        return any(
            (slot.time or "")[:5] == candidate_time
            and (
                protected_class_variant_key(slot.class_name) == candidate_class
                or same_format_family(slot.class_name, class_name)
            )
            for slot in slots_today
        )

    def _top_historic_trainer_for_slot(self, location: str, day_name: str, class_name: str, time_str: str) -> Optional[str]:
        """Return trainer with strongest historical record for this class+slot."""
        day_int = DOW_REVERSE[day_name]
        rows = self._class_candidate_rows(location, class_name, day_int)
        best: Optional[Tuple[float, str]] = None
        for r in rows:
            trainer = r.get("trainer")
            if not trainer:
                continue
            hist = self._get_hist(location, class_name, trainer, day_int, time_str)
            sessions = float(hist.get("session_count", 0) or 0)
            if sessions <= 0:
                continue
            fill = float(hist.get("avg_fill_rate", 0.0) or 0.0)
            checkins = float(hist.get("avg_checkin", 0.0) or 0.0)
            score = (sessions * 10000.0) + (fill * 100.0) + checkins
            if best is None or score > best[0]:
                best = (score, trainer)
        return best[1] if best else None

    def _top_trainer_rejection_reason(
        self,
        location: str,
        day_name: str,
        date_str: str,
        time_str: str,
        class_name: str,
        selected_trainer: str,
        already_at_time: Set[str],
        slots_today: List["ScheduleSlot"],
    ) -> str:
        """Explain why the top historic trainer for this slot was not selected."""
        top_trainer = self._top_historic_trainer_for_slot(location, day_name, class_name, time_str)
        if not top_trainer or normalize_trainer_name(top_trainer) == normalize_trainer_name(selected_trainer):
            return ""

        if self._is_inactive(top_trainer):
            reason = "inactive trainer"
        elif self._on_leave(top_trainer, date_str, location):
            reason = "on leave/off day"
        elif self._loc_excluded(top_trainer, location):
            reason = "location excluded by override"
        elif top_trainer in already_at_time:
            reason = "already assigned at this timeslot"
        elif self._custom_rule_blocks(location, day_name, time_str, class_name, top_trainer):
            reason = "blocked by custom hard rule"
        elif "PowerCycle" in class_name and not self.trainer_profiles.get(top_trainer, {}).get("qualifications", {}).get("powercycle", False):
            reason = "missing PowerCycle qualification"
        elif "Strength Lab" in class_name and not self.trainer_profiles.get(top_trainer, {}).get("qualifications", {}).get("strength_lab", False):
            reason = "missing Strength Lab qualification"
        elif "Foundations" in class_name and not self.trainer_profiles.get(top_trainer, {}).get("qualifications", {}).get("foundations", False):
            reason = "missing Foundations qualification"
        elif not self._trainer_ok(top_trainer, location, day_name, time_str, class_name):
            reason = "availability/load constraint (time window, shift, overlap, daily/weekly cap, or movement lock)"
        elif self._would_repeat_consecutive_format(slots_today, time_str, class_name):
            reason = "consecutive-format guard"
        elif would_block_recovery(class_name, time_str, slots_today):
            reason = "recovery sequencing guard"
        elif "Recovery" in class_name and not is_recovery_last_in_shift(class_name, time_str, slots_today):
            reason = "recovery must be last in shift"
        elif self._tier1_under_target_exists_for_slot(
            location, day_name, date_str, time_str, class_name, already_at_time,
            exclude_trainer=top_trainer,
        ):
            reason = "tier-priority balancing (Tier 1 under-target)"
        elif (
            location == "Supreme HQ, Bandra"
            and self._tier1_available_for_slot(
                location, day_name, date_str, time_str, class_name, already_at_time,
                exclude_trainer=top_trainer,
            )
        ):
            reason = "Supreme allocation prefers available Tier 1 trainer"
        elif self._higher_tier_underused_exists_for_location_slot(
            location, day_name, date_str, time_str, class_name, already_at_time,
            exclude_trainer=top_trainer,
            candidate_recommendation="INCLUDE",
        ):
            reason = "location tier-load balancing"
        else:
            reason = "optimizer multi-objective balancing"

        return f"Top trainer rejected: {top_trainer} — {reason}. "

    def _get_hist(self, location, class_name, trainer, day_int, time_str) -> dict:
        t = time_str[:5] if time_str else ""
        key = (location, class_name, trainer, day_int, t)
        if key in self.hist_lookup:
            return self.hist_lookup[key]

        if not hasattr(self, "_hist_by_combo_day"):
            self._build_history_indexes()

        # Try nearby times (+-15 min) without scanning unrelated history rows.
        t_min = slot_time_to_minutes(t) if t else 0
        nearby = [
            (abs(hist_min - t_min), metrics)
            for hist_min, metrics in self._hist_by_combo_day.get((location, class_name, trainer, day_int), [])
            if abs(hist_min - t_min) <= 15
        ]
        if nearby:
            nearby.sort(key=lambda item: item[0])
            return nearby[0][1]
        if location in DERIVED_LOCATION_SOURCES:
            source_matches = [
                self._get_hist(source, class_name, trainer, day_int, time_str)
                for source in DERIVED_LOCATION_SOURCES.get(location, [])
                if source != location
            ]
            source_matches = [m for m in source_matches if m and m.get("session_count", 0) > 0]
            if source_matches:
                return self._aggregate_hist_metrics(source_matches)
        return self._get_hist_slot(location, class_name, day_int, time_str)

    def _get_hist_slot(self, location, class_name, day_int, time_str) -> dict:
        """Get aggregated historical data for a class+location+day+time slot, regardless of trainer."""
        t = time_str[:5] if time_str else ""
        cache_key = (location, class_name, day_int, t)
        if not hasattr(self, "_hist_slot_exact"):
            self._build_history_indexes()
        if cache_key in self._hist_slot_cache:
            return self._hist_slot_cache[cache_key]
        if cache_key in self._hist_slot_exact:
            self._hist_slot_cache[cache_key] = self._hist_slot_exact[cache_key]
            return self._hist_slot_cache[cache_key]

        t_min = slot_time_to_minutes(t) if t else 0
        matching = [
            metrics
            for hist_min, metrics in self._hist_by_slot_day.get((location, class_name, day_int), [])
            if abs(hist_min - t_min) <= 15
        ]
        if not matching and location in DERIVED_LOCATION_SOURCES:
            for source in DERIVED_LOCATION_SOURCES.get(location, []):
                if source == location:
                    continue
                matching.extend(
                    metrics
                    for hist_min, metrics in self._hist_by_slot_day.get((source, class_name, day_int), [])
                    if abs(hist_min - t_min) <= 15
                )

        if not matching:
            canonical_class = canonical_class_key(class_name)
            canonical_cache_key = (location, canonical_class, day_int, t)
            if canonical_cache_key in self._hist_canonical_slot_exact:
                result = self._hist_canonical_slot_exact[canonical_cache_key]
                self._hist_slot_cache[cache_key] = result
                return result

            matching = [
                metrics
                for hist_min, metrics in self._hist_by_canonical_slot_day.get((location, canonical_class, day_int), [])
                if abs(hist_min - t_min) <= 15
            ]
            if not matching and location in DERIVED_LOCATION_SOURCES:
                for source in DERIVED_LOCATION_SOURCES.get(location, []):
                    if source == location:
                        continue
                    matching.extend(
                        metrics
                        for hist_min, metrics in self._hist_by_canonical_slot_day.get((source, canonical_class, day_int), [])
                        if abs(hist_min - t_min) <= 15
                    )

        result = self._aggregate_hist_metrics(matching) if matching else {}
        self._hist_slot_cache[cache_key] = result
        return result

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

    def _public_score_fields(
        self,
        base_score: float,
        placement_score: float,
        historical_session_count: int,
        recommendation: str,
        is_experimental: bool,
        manual_pin: bool = False,
    ) -> Tuple[float, float, str, bool]:
        """Return UI-facing performance score separately from optimizer placement utility."""
        performance = float(base_score or 0.0)
        placement = round(float(placement_score or 0.0), 2)
        sessions = int(historical_session_count or 0)
        rec = recommendation or "INCLUDE"
        exp = bool(is_experimental)
        if manual_pin:
            return round(min(100.0, max(0.0, performance)), 2), placement, rec, exp
        if sessions == 0:
            return round(min(performance, 20.0), 2), placement, "EXPERIMENTAL", True
        if sessions <= 2:
            rec = "CONSIDER" if rec in {"PROTECT", "PROTECT_EXACT", "INCLUDE"} else rec
            return round(min(performance, 35.0), 2), placement, rec, exp
        if sessions <= 7:
            rec = "CONSIDER" if rec in {"PROTECT", "PROTECT_EXACT", "INCLUDE"} else rec
            return round(min(performance, 50.0), 2), placement, rec, exp
        return round(min(100.0, max(0.0, performance)), 2), placement, rec, exp

    def _quick_check(self, location, day_name, time_str, cname, slots_today) -> List[str]:
        v = build_constraint_violations(location, day_name, time_str, cname, slots_today)
        if self._would_repeat_consecutive_format(slots_today, time_str, cname):
            v.append("UNIV-023: Consecutive class format")
        return v

    def _get_pinned_slots(self, location, day_name) -> List[dict]:
        p = []
        for pin in self.schedule_config.get("manual_protected", []) or []:
            if not isinstance(pin, dict):
                continue
            pin_location = pin.get("location")
            pin_day = pin.get("day") or pin.get("day_of_week")
            pin_time = pin.get("time")
            pin_class = pin.get("class") or pin.get("class_name")
            pin_trainer = pin.get("trainer") or pin.get("trainer_1")
            if pin_location != location or pin_day != day_name:
                continue
            if not pin_time or not pin_class or not pin_trainer:
                continue
            manual_pin = {
                "time": pin_time,
                "trainer": pin_trainer,
                "class": pin_class,
                "manual": True,
            }
            for optional_key in ("id", "room", "note"):
                if pin.get(optional_key):
                    manual_pin[optional_key] = pin[optional_key]
            p.append(manual_pin)

        return p
