"""
Agent 3 — Class Scorer (CSV-based, trust-weighted)
Uses 'Class Performance by UD1.csv' to define slot performance and
'Class Performance by Trainer.csv' for trainer options within those slots.
Uses absolute thresholds — fixes the normalization problem where 38% and 78%
fill both score INCLUDE.
"""
import json
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from agents.io_utils import atomic_write_json

STATE_DIR = Path("state")

# CSV column interpretation (column names are misleading — see comments)
COL_TRAINER = "Trainer"
COL_CLASS = "Class"
COL_LOCATION = "Location"
COL_DAY = "Day"
COL_TIME = "Time"
COL_CHECKIN = "CheckedIn"
COL_CAPACITY = "Capacity"
COL_REVENUE = "Revenue"
COL_UID1 = "UniqueID1"
COL_UID2 = "UniqueID2"

# Pre-aggregated trainer×class×location stats (same value for all rows in group)
# NOTE: CSV column names are misleading — actual contents documented below
COL_CLASSES = "Classes"              # total sessions this trainer ran this class at this location
COL_AVG_CI_INCL = "ClassAvgInclEmpty"  # avg checkin incl zero-attendance sessions (labelled correctly)
COL_FILL_RATE = "ClassAvgExclEmpty"  # ACTUAL fill rate as "XX.XX%" (mislabelled in CSV)
COL_REV_PER_SESSION = "FillRate"     # avg revenue per session INR (mislabelled as FillRate in CSV)

# Absolute recommendation thresholds (not percentile — prevents 38%==78% problem)
PROTECT_SCORE = 70
PROTECT_SESSIONS = 8
INCLUDE_SCORE = 50
INCLUDE_SESSIONS = 5
CONSIDER_SCORE = 25
CONSIDER_SESSIONS = 3
AUTO_PROTECT_MIN_SESSIONS = 8  # Min sessions to auto-protect above-studio-avg combos
TOP_PERFORMER_PROTECT_FILL = 0.50
TOP_PERFORMER_ATTENDANCE_MULTIPLE = 1.25

PINNED_SCORE = 85
PINNED_SESSIONS = 10
SCORING_WEIGHTS = {
    "avg_attendance": 0.75,
    "capacity_fill": 0.15,
    "revenue": 0.07,
    "sessions": 0.03,
}
EXCLUDED_CLASS_KEYWORDS = (
    "hosted class",
    "pre/post natal",
    "pre post natal",
    "foundations",
    "sweat in 30",
    "unknown class",
)


def _is_strength_lab_protected(class_name: str, fill_rate: float, session_count: int) -> bool:
    return (
        "strength lab" in str(class_name or "").lower()
        and float(fill_rate or 0.0) > 0.50
        and int(session_count or 0) >= CONSIDER_SESSIONS
    )


def _uses_fill_rate_for_protection(class_name: str) -> bool:
    lower = str(class_name or "").lower()
    return "powercycle" in lower or "strength" in lower


def _attendance_group_key(class_name: str) -> str:
    lower = str(class_name or "").lower()
    if "powercycle" in lower or "power cycle" in lower:
        return "powercycle"
    if "strength lab" in lower:
        return "strength_lab"
    if "cardio barre" in lower:
        return "cardio_barre"
    if "mat 57" in lower or "mat57" in lower:
        return "mat_57"
    if "fit" in lower:
        return "fit"
    if "barre 57" in lower or "power barre" in lower or "barre fusion" in lower:
        return "barre_57"
    if "back body" in lower:
        return "back_body_blaze"
    if "amped" in lower:
        return "amped_up"
    if "hiit" in lower:
        return "hiit"
    if "recovery" in lower or "flex" in lower:
        return "recovery"
    return lower.strip() or "other"


def _is_top_performer_protected(
    class_name: str,
    avg_attendance: float,
    studio_avg_attendance: float,
    fill_rate: float,
    session_count: int,
) -> bool:
    if int(session_count or 0) < AUTO_PROTECT_MIN_SESSIONS:
        return False
    if _uses_fill_rate_for_protection(class_name):
        return float(fill_rate or 0.0) > TOP_PERFORMER_PROTECT_FILL
    attendance = float(avg_attendance or 0.0)
    studio_avg = float(studio_avg_attendance or 0.0)
    return (
        (studio_avg > 0.0 and attendance > studio_avg and float(fill_rate or 0.0) > TOP_PERFORMER_PROTECT_FILL)
        or (studio_avg > 0.0 and attendance >= studio_avg * TOP_PERFORMER_ATTENDANCE_MULTIPLE)
    )

PERMITTED_LOCATIONS = {
    "Kwality House, Kemps Corner",
    "Supreme HQ, Bandra",
    "Kenkere House",
    "Copper & Cloves",
}


def _copper_class_name(row) -> str:
    session = str(row.get("SessionName", "") or "").lower()
    class_name = str(row.get(COL_CLASS, "") or "")
    source = f"{session} {class_name.lower()}"
    if "fit" in source:
        return "Copper + Cloves FIT"
    if "mat 57" in source or "mat57" in source:
        return "Copper + Cloves Mat 57"
    return "Copper + Cloves Barre 57"


def _parse_pct(val) -> float:
    """Parse "34.44%" → 0.3444. Returns 0.0 on failure."""
    if pd.isna(val):
        return 0.0
    s = str(val).strip().rstrip("%")
    try:
        return float(s) / 100.0
    except ValueError:
        return 0.0


def _normalize_within_location(records: list, key: str, out_key: str, locations: list):
    for loc in locations:
        vals = [r[key] for r in records if r.get("location", r.get(COL_LOCATION)) == loc]
        if not vals:
            continue
        mn, mx = min(vals), max(vals)
        if mx == mn:
            for r in records:
                if r.get("location", r.get(COL_LOCATION)) == loc:
                    r[out_key] = 0.5
            continue
        rng = mx - mn
        for r in records:
            if r.get("location", r.get(COL_LOCATION)) == loc:
                r[out_key] = (r[key] - mn) / rng


def _recommendation(score: float, slot_sessions: int) -> str:
    if score >= PROTECT_SCORE and slot_sessions >= PROTECT_SESSIONS:
        return "PROTECT"
    if score >= INCLUDE_SCORE and slot_sessions >= INCLUDE_SESSIONS:
        return "INCLUDE"
    if score >= CONSIDER_SCORE and slot_sessions >= CONSIDER_SESSIONS:
        return "CONSIDER"
    return "DROP"


def _apply_protect_score_floor(record: dict) -> None:
    if record.get("recommendation") != "PROTECT":
        return
    breakdown = record.get("score_breakdown")
    if isinstance(breakdown, dict):
        breakdown["policy_protection"] = True


class ClassScorer:
    def __init__(self, weights: dict = None, csv_path: str = None):
        self.weights = weights or SCORING_WEIGHTS
        self.csv_path = csv_path or "Class Performance by UD1.csv"

    def _inactive_trainers(self) -> set:
        inactive = set()
        for path in (Path("config/trainer_overrides.json"), Path("config/schedule_config.json")):
            if not path.exists():
                continue
            try:
                with open(path) as f:
                    data = json.load(f)
                inactive.update(data.get("inactive_trainers", []))
            except Exception:
                continue
        profiles_path = Path("rules/trainer_profiles.json")
        if profiles_path.exists():
            try:
                with open(profiles_path) as f:
                    profiles = json.load(f)
                inactive.update(
                    p.get("name")
                    for p in profiles
                    if p.get("name") and p.get("active") is False
                )
            except Exception:
                pass
        return inactive

    def run(self) -> dict:
        print("[Agent 3] Scorer starting (historic UD1-first mode)...")

        def _clean_time(t):
            s = str(t).strip()
            m = re.match(r"^(\d{1,2}):(\d{2})", s)
            if m:
                return f"{int(m.group(1)):02d}:{m.group(2)}"
            return s

        def _load_performance_csv(csv_file: Path, label: str) -> pd.DataFrame:
            df = pd.read_csv(csv_file, low_memory=False)
            print(f"  Loaded {len(df):,} rows from {label}")

            session_names = df["SessionName"] if "SessionName" in df.columns else ""
            copper_mask = (
                df[COL_LOCATION].astype(str).str.strip().str.lower().eq("pop-up")
                & pd.Series(session_names, index=df.index).astype(str).str.contains("copper", case=False, na=False)
            )
            df.loc[copper_mask, COL_LOCATION] = "Copper & Cloves"
            df.loc[copper_mask, COL_CLASS] = df.loc[copper_mask].apply(_copper_class_name, axis=1)

            df = df[df[COL_LOCATION].isin(PERMITTED_LOCATIONS)].copy()
            print(f"  After location filter [{label}]: {len(df):,} rows")

            df["time_clean"] = df[COL_TIME].apply(_clean_time)
            df[COL_CHECKIN] = pd.to_numeric(df[COL_CHECKIN], errors="coerce").fillna(0)
            df[COL_CAPACITY] = pd.to_numeric(df[COL_CAPACITY], errors="coerce").fillna(1).clip(lower=1)
            df[COL_REVENUE] = pd.to_numeric(df[COL_REVENUE], errors="coerce").fillna(0)
            df["fill_row"] = (df[COL_CHECKIN] / df[COL_CAPACITY]).clip(upper=1.0)

            if COL_UID1 not in df.columns:
                df[COL_UID1] = (
                    df[COL_LOCATION].astype(str) + "|" + df[COL_CLASS].astype(str) + "|" +
                    df[COL_DAY].astype(str) + "|" + df["time_clean"].astype(str)
                )
            else:
                df[COL_UID1] = df[COL_UID1].astype(str)
            if COL_UID2 not in df.columns:
                df[COL_UID2] = df[COL_UID1].astype(str) + "|" + df[COL_TRAINER].astype(str)
            else:
                df[COL_UID2] = df[COL_UID2].astype(str)
            return df

        def _exclude_non_schedulable_classes(df: pd.DataFrame) -> pd.DataFrame:
            if df.empty:
                return df.copy()
            class_lower = df[COL_CLASS].astype(str).str.lower()
            excluded_mask = class_lower.apply(lambda x: any(k in x for k in EXCLUDED_CLASS_KEYWORDS))
            return df[~excluded_mask].copy()

        def _exclude_inactive_trainers(df: pd.DataFrame, inactive: set) -> pd.DataFrame:
            if df.empty or not inactive:
                return df.copy()
            return df[~df[COL_TRAINER].isin(inactive)].copy()

        def _prepare_scoring_metrics(df: pd.DataFrame) -> pd.DataFrame:
            df = df.copy()
            if df.empty:
                df[COL_CLASSES] = pd.Series(dtype=int)
                df[COL_AVG_CI_INCL] = pd.Series(dtype=float)
                df["trainer_fill"] = pd.Series(dtype=float)
                df["trainer_rev"] = pd.Series(dtype=float)
                return df
            raw_session_source = COL_AVG_CI_INCL not in df.columns or COL_FILL_RATE not in df.columns
            if raw_session_source:
                uid2_groups = df.groupby(COL_UID2, dropna=False)
                df[COL_CLASSES] = uid2_groups[COL_CHECKIN].transform("size").astype(int)
                df[COL_AVG_CI_INCL] = uid2_groups[COL_CHECKIN].transform("mean")
                checked_sum = uid2_groups[COL_CHECKIN].transform("sum")
                capacity_sum = uid2_groups[COL_CAPACITY].transform("sum").clip(lower=1)
                revenue_mean = uid2_groups[COL_REVENUE].transform("mean")
                df["trainer_fill"] = (checked_sum / capacity_sum).clip(upper=1.0)
                df["trainer_rev"] = revenue_mean
            else:
                df[COL_CLASSES] = pd.to_numeric(df[COL_CLASSES], errors="coerce").fillna(0).astype(int)
                df[COL_AVG_CI_INCL] = pd.to_numeric(df[COL_AVG_CI_INCL], errors="coerce").fillna(0)
                df["trainer_fill"] = df[COL_FILL_RATE].apply(_parse_pct).clip(upper=1.0)
                df["trainer_rev"] = pd.to_numeric(df[COL_REV_PER_SESSION], errors="coerce").fillna(0)
            return df

        # -----------------------------------------------------------------
        # 1. Load slot and trainer sources
        # -----------------------------------------------------------------
        csv_file = Path(self.csv_path)
        if not csv_file.exists():
            raise FileNotFoundError(
                f"CSV not found: {self.csv_path}. "
                "Pass --csv or place file in project root."
            )

        inactive = self._inactive_trainers()
        slot_df = _prepare_scoring_metrics(_exclude_non_schedulable_classes(_load_performance_csv(csv_file, self.csv_path)))

        trainer_csv_file = Path("Class Performance by Trainer.csv")
        trainer_source = trainer_csv_file if trainer_csv_file.exists() else csv_file
        trainer_label = str(trainer_source)
        trainer_df = _prepare_scoring_metrics(
            _exclude_inactive_trainers(
                _exclude_non_schedulable_classes(_load_performance_csv(trainer_source, trainer_label)),
                inactive,
            )
        )

        if not slot_df.empty:
            valid_uid1 = set(slot_df[COL_UID1].astype(str))
            trainer_df = trainer_df[trainer_df[COL_UID1].astype(str).isin(valid_uid1)].copy()

        # -----------------------------------------------------------------
        # New scoring model:
        # 1) UniqueID1 ranks the class/location/day/time slot independent of trainer.
        # 2) UniqueID2 ranks trainers within that class slot for assignment.
        # -----------------------------------------------------------------
        trainer_group_cols = [COL_UID2, COL_UID1, COL_LOCATION, COL_CLASS, COL_TRAINER, COL_DAY, "time_clean"]
        trainer_agg = trainer_df.groupby(trainer_group_cols, dropna=False).agg(
            session_count=(COL_CLASSES, "max"),
            avg_attendance=(COL_AVG_CI_INCL, "max"),
            fill_rate=("trainer_fill", "max"),
            revenue_per_session=("trainer_rev", "max"),
        ).reset_index()
        trainer_agg["total_revenue"] = trainer_agg["session_count"] * trainer_agg["revenue_per_session"]

        def _weighted_avg(group, value_col):
            weights = group["session_count"].clip(lower=0)
            if float(weights.sum()) <= 0:
                return float(group[value_col].mean()) if len(group) else 0.0
            return float(np.average(group[value_col], weights=weights))

        def _weighted_records_avg(records: list, value_key: str) -> float:
            weights = [max(0, int(r.get("session_count", 0) or 0)) for r in records]
            values = [float(r.get(value_key, 0.0) or 0.0) for r in records]
            total_weight = sum(weights)
            if total_weight <= 0:
                return sum(values) / max(len(values), 1)
            return sum(v * w for v, w in zip(values, weights)) / total_weight

        group_records = []
        for uid1, grp in slot_df.groupby(COL_UID1, dropna=False):
            first = grp.iloc[0]
            sessions = int(pd.to_numeric(grp[COL_CLASSES], errors="coerce").fillna(0).sum())
            session_weights = pd.to_numeric(grp[COL_CLASSES], errors="coerce").fillna(0).clip(lower=0)
            if float(session_weights.sum()) <= 0:
                avg_attendance = float(pd.to_numeric(grp[COL_AVG_CI_INCL], errors="coerce").fillna(0).mean())
                avg_fill_rate = float(pd.to_numeric(grp["trainer_fill"], errors="coerce").fillna(0).mean())
                avg_revenue = float(pd.to_numeric(grp["trainer_rev"], errors="coerce").fillna(0).mean())
            else:
                avg_attendance = float(np.average(pd.to_numeric(grp[COL_AVG_CI_INCL], errors="coerce").fillna(0), weights=session_weights))
                avg_fill_rate = float(np.average(pd.to_numeric(grp["trainer_fill"], errors="coerce").fillna(0), weights=session_weights))
                avg_revenue = float(np.average(pd.to_numeric(grp["trainer_rev"], errors="coerce").fillna(0), weights=session_weights))
            group_records.append({
                "unique_id_1": str(uid1),
                "location": first[COL_LOCATION],
                "class": first[COL_CLASS],
                "trainer": first.get(COL_TRAINER, ""),
                "day_name": str(first[COL_DAY]).strip(),
                "time": first["time_clean"],
                "session_count": sessions,
                "avg_attendance": round(avg_attendance, 2),
                "avg_fill_rate": round(avg_fill_rate, 4),
                "avg_revenue": round(avg_revenue, 2),
                "total_revenue": round(float((pd.to_numeric(grp[COL_CLASSES], errors="coerce").fillna(0) * pd.to_numeric(grp["trainer_rev"], errors="coerce").fillna(0)).sum()), 2),
                "trainer_count": int(grp[COL_TRAINER].nunique()),
            })

        locations = list(slot_df[COL_LOCATION].unique()) if not slot_df.empty else list(trainer_df[COL_LOCATION].unique())
        _normalize_within_location(group_records, "avg_attendance", "avg_attendance_norm", locations)
        _normalize_within_location(group_records, "avg_revenue", "revenue_norm", locations)
        _normalize_within_location(group_records, "session_count", "sessions_norm", locations)

        # For non-PowerCycle/non-Strength formats, attendance should be evaluated
        # at grouped-class level (family bucket) rather than exact slot granularity.
        group_attendance_by_loc_cls = {}
        for loc in locations:
            loc_recs = [r for r in group_records if r["location"] == loc]
            if not loc_recs:
                continue
            by_cls = defaultdict(list)
            for rec in loc_recs:
                by_cls[_attendance_group_key(rec.get("class", ""))].append(rec)
            for cls_key, cls_recs in by_cls.items():
                total_sessions = sum(int(x.get("session_count", 0) or 0) for x in cls_recs)
                if total_sessions <= 0:
                    avg_attendance = sum(float(x.get("avg_attendance", 0.0) or 0.0) for x in cls_recs) / max(len(cls_recs), 1)
                else:
                    avg_attendance = (
                        sum(float(x.get("avg_attendance", 0.0) or 0.0) * int(x.get("session_count", 0) or 0) for x in cls_recs)
                        / total_sessions
                    )
                group_attendance_by_loc_cls[(loc, cls_key)] = float(avg_attendance)

        loc_group_values = defaultdict(list)
        for (loc, _cls), val in group_attendance_by_loc_cls.items():
            loc_group_values[loc].append(val)
        loc_group_min_max = {}
        for loc, vals in loc_group_values.items():
            if not vals:
                continue
            loc_group_min_max[loc] = (min(vals), max(vals))

        for r in group_records:
            cls_key = _attendance_group_key(r.get("class", ""))
            grouped_attendance = float(group_attendance_by_loc_cls.get((r["location"], cls_key), r.get("avg_attendance", 0.0)))
            r["grouped_avg_attendance"] = round(grouped_attendance, 2)
            mn_mx = loc_group_min_max.get(r["location"])
            if not mn_mx:
                r["grouped_avg_attendance_norm"] = 0.5
            else:
                mn, mx = mn_mx
                if mx == mn:
                    r["grouped_avg_attendance_norm"] = 0.5
                else:
                    r["grouped_avg_attendance_norm"] = (grouped_attendance - mn) / (mx - mn)

        studio_avg_attendance = {}
        for loc in locations:
            loc_recs = [r for r in group_records if r["location"] == loc]
            if loc_recs:
                studio_avg_attendance[loc] = _weighted_records_avg(loc_recs, "avg_attendance")

        day_name_map = {
            "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
            "Friday": 4, "Saturday": 5, "Sunday": 6,
        }

        PRIME_AM = {"07:30","07:45","08:00","08:15","08:30","08:45","09:00","09:15","09:30","09:45"}
        PRIME_MID = {"11:00","11:15","11:30","11:45","12:00"}
        PRIME_PM = {"18:00","18:15","18:30","18:45","19:00","19:15","19:30"}

        def _class_family_mode(class_name: str) -> str:
            lower = str(class_name or "").lower()
            if "powercycle" in lower or "power cycle" in lower:
                return "powercycle"
            if "strength lab" in lower or "strength" in lower:
                return "strength"
            return "default"

        # Per-location baseline by family mode: default uses attendance, strength/family uses fill.
        family_loc_baseline: dict = {}
        family_loc_values: dict = defaultdict(list)
        for _r in group_records:
            _fam = _class_family_mode(_r.get("class", ""))
            _loc = _r.get("location", "")
            _metric = float(_r.get("avg_fill_rate", 0.0)) if _fam in {"strength", "powercycle"} else float(_r.get("avg_attendance", 0.0))
            family_loc_values[(_loc, _fam)].append((_metric, int(_r.get("session_count", 0) or 0)))
        for _k, _vals in family_loc_values.items():
            total_weight = sum(max(0, weight) for _, weight in _vals)
            if total_weight <= 0:
                family_loc_baseline[_k] = (sum(value for value, _ in _vals) / len(_vals)) if _vals else 0.0
            else:
                family_loc_baseline[_k] = sum(value * max(0, weight) for value, weight in _vals) / total_weight

        def _score_record(r):
            w = self.weights
            grouped_attendance_norm = float(r.get("grouped_avg_attendance_norm", r.get("avg_attendance_norm", 0.0)))
            grouped_attendance_raw = float(r.get("grouped_avg_attendance", r.get("avg_attendance", 0.0)))
            fam_mode = _class_family_mode(r.get("class", ""))
            if fam_mode == "powercycle":
                fill = float(r.get("avg_fill_rate", 0.0))
                att_norm = float(r.get("avg_attendance_norm", 0.0))
                ses_norm = float(r.get("sessions_norm", 0.0))
                composite = (0.50 * fill) + (0.30 * att_norm) + (0.20 * ses_norm)
                score = round(float(np.clip(composite * 100.0, 0, 100)), 2)
                return score, {
                    "total_score": score,
                    "base_score": score,
                    "recency_boost": 0.0,
                    "formula": "PowerCycle weighted score = fill_rate*0.50 + avg_attendance_norm*0.30 + sessions_norm*0.20",
                    "components": [
                        {"key": "capacity_fill", "label": "Capacity Fill Rate", "weight": 0.50, "raw_value": round(fill, 4), "normalized_value": round(fill, 4), "points": round(fill * 50, 2), "max_points": 50.0},
                        {"key": "avg_attendance_norm", "label": "Average Attendance (norm)", "weight": 0.30, "raw_value": round(float(r.get("avg_attendance", 0.0)), 2), "normalized_value": round(att_norm, 4), "points": round(att_norm * 30, 2), "max_points": 30.0},
                        {"key": "sessions_norm", "label": "Sample Size (norm)", "weight": 0.20, "raw_value": int(r.get("session_count", 0)), "normalized_value": round(ses_norm, 4), "points": round(ses_norm * 20, 2), "max_points": 20.0},
                    ],
                }
            if fam_mode == "strength":
                # Fill-rate first for strength classes.
                fill = float(r.get("avg_fill_rate", 0.0))
                att_norm = float(r.get("avg_attendance_norm", 0.0))
                ses_norm = float(r.get("sessions_norm", 0.0))
                score = round(float(np.clip((fill * 0.70 + att_norm * 0.20 + ses_norm * 0.10) * 100.0, 0, 100)), 2)
                return score, {
                    "total_score": score,
                    "base_score": score,
                    "recency_boost": 0.0,
                    "formula": "Strength score = fill_rate*0.70 + avg_attendance_norm*0.20 + sessions_norm*0.10",
                    "components": [
                        {"key": "capacity_fill", "label": "Capacity Fill Rate", "weight": 0.70, "raw_value": round(fill, 4), "normalized_value": round(fill, 4), "points": round(fill * 70, 2), "max_points": 70.0},
                        {"key": "avg_attendance_norm", "label": "Average Attendance (norm)", "weight": 0.20, "raw_value": round(float(r.get("avg_attendance", 0.0)), 2), "normalized_value": round(att_norm, 4), "points": round(att_norm * 20, 2), "max_points": 20.0},
                        {"key": "sessions_norm", "label": "Sample Size (norm)", "weight": 0.10, "raw_value": int(r.get("session_count", 0)), "normalized_value": round(ses_norm, 4), "points": round(ses_norm * 10, 2), "max_points": 10.0},
                    ],
                }
            attendance_weight = w["avg_attendance"]
            fill_weight = w["capacity_fill"]
            revenue_weight = w["revenue"]
            sessions_weight = w["sessions"]
            attendance_norm_for_score = grouped_attendance_norm
            attendance_raw_for_score = grouped_attendance_raw
            attendance_points = attendance_norm_for_score * attendance_weight * 100
            fill_points = float(r.get("avg_fill_rate", 0.0)) * fill_weight * 100
            revenue_points = float(r.get("revenue_norm", 0.0)) * revenue_weight * 100
            session_points = float(r.get("sessions_norm", 0.0)) * sessions_weight * 100
            base = attendance_points + fill_points + revenue_points + session_points
            # Prime slot bonus: classes at peak times get a reliability boost
            t = str(r.get("time", "")).strip()[:5]
            prime_bonus = 8.0 if t in PRIME_AM else (6.0 if t in PRIME_MID else (7.0 if t in PRIME_PM else 0.0))
            # Volume reliability bonus: more sessions = more confident prediction (max +5)
            sc = int(r.get("session_count", 0))
            volume_bonus = min(5.0, sc / 12.0)
            # Fill-rate absolute floor bonus: if avg fill > 60% it's genuinely performing
            fill_val = float(r.get("avg_fill_rate", 0.0))
            fill_abs_bonus = 6.0 if fill_val >= 0.60 else (3.0 if fill_val >= 0.45 else 0.0)
            score = round(float(np.clip(base + prime_bonus + volume_bonus + fill_abs_bonus, 0, 100)), 2)
            return score, {
                "total_score": score,
                "base_score": round(base, 2),
                "recency_boost": round(prime_bonus + volume_bonus + fill_abs_bonus, 2),
                "bonus_components": [
                    {
                        "key": "prime_slot",
                        "label": "Prime Time Bonus",
                        "raw_value": t,
                        "points": round(prime_bonus, 2),
                        "max_points": 7.0,
                        "explanation": "+8 pts for peak AM (07:30-10:00), +6 for midday (11:00-12:00), +7 for peak PM (18:00-19:30) slots.",
                    },
                    {
                        "key": "volume_reliability",
                        "label": "Volume Reliability",
                        "raw_value": sc,
                        "points": round(volume_bonus, 2),
                        "max_points": 5.0,
                        "explanation": "Up to +5 pts for stronger historical volume.",
                    },
                    {
                        "key": "fill_abs",
                        "label": "Fill Rate Absolute Bonus",
                        "raw_value": round(fill_val, 4),
                        "points": round(fill_abs_bonus, 2),
                        "max_points": 6.0,
                        "explanation": "+6 pts if avg fill >= 60%, +3 if >= 45%.",
                    },
                ],
                "formula": (
                    "Default class score = avg_attendance_norm*75 + avg_fill_rate*15 + "
                    "revenue_norm*7 + sessions_norm*3 + prime_bonus + volume_bonus + fill_abs_bonus. "
                    "PowerCycle and Strength use their separate fill-first formulas."
                ),
                "components": [
                    {
                        "key": "avg_attendance",
                        "label": "Average Attendance",
                        "weight": attendance_weight,
                        "raw_value": round(attendance_raw_for_score, 2),
                        "normalized_value": round(attendance_norm_for_score, 4),
                        "points": round(attendance_points, 2),
                        "max_points": round(attendance_weight * 100, 2),
                        "explanation": "Primary scoring signal for non-PowerCycle/non-Strength classes; uses grouped-class average attendance within the studio.",
                    },
                    {
                        "key": "capacity_fill",
                        "label": "Capacity Fill Rate",
                        "weight": fill_weight,
                        "raw_value": round(float(r.get("avg_fill_rate", 0.0)), 4),
                        "normalized_value": round(float(r.get("avg_fill_rate", 0.0)), 4),
                        "points": round(fill_points, 2),
                        "max_points": round(fill_weight * 100, 2),
                        "explanation": "Primary scoring signal for PowerCycle and Strength; secondary signal for other classes.",
                    },
                    {
                        "key": "revenue",
                        "label": "Revenue Generated",
                        "weight": revenue_weight,
                        "raw_value": round(float(r.get("avg_revenue", 0.0)), 2),
                        "normalized_value": round(float(r.get("revenue_norm", 0.0)), 4),
                        "points": round(revenue_points, 2),
                        "max_points": round(revenue_weight * 100, 2),
                        "explanation": "Revenue per session normalized against other slots in the same studio.",
                    },
                    {
                        "key": "sessions",
                        "label": "Number of Sessions",
                        "weight": sessions_weight,
                        "raw_value": int(r.get("session_count", 0)),
                        "normalized_value": round(float(r.get("sessions_norm", 0.0)), 4),
                        "points": round(session_points, 2),
                        "max_points": round(sessions_weight * 100, 2),
                        "explanation": "Lowest-weight confidence/history component.",
                    },
                ],
            }

        for r in group_records:
            r["day"] = day_name_map.get(r["day_name"], -1)
            r["score"], r["score_breakdown"] = _score_record(r)
            r["base_score"] = r["score_breakdown"]["base_score"]
            r["recency_boost"] = r["score_breakdown"]["recency_boost"]
            r["recommendation"] = _recommendation(r["score"], int(r["session_count"]))
            if int(r["session_count"]) < CONSIDER_SESSIONS:
                r["recommendation"] = "DROP"
            strength_protect = _is_strength_lab_protected(r["class"], r["avg_fill_rate"], r["session_count"])
            fam_mode = _class_family_mode(r["class"])
            baseline = family_loc_baseline.get((r["location"], fam_mode), 0.0)
            if fam_mode in {"strength", "powercycle"}:
                top_performer_protect = int(r["session_count"]) >= 8 and float(r["avg_fill_rate"]) > float(baseline)
            else:
                top_performer_protect = int(r["session_count"]) >= 8 and float(r["avg_attendance"]) > float(baseline)
            r["pinned_slot"] = strength_protect or top_performer_protect
            r["protect_class_time"] = strength_protect or top_performer_protect
            if (strength_protect or top_performer_protect) and r["recommendation"] == "PROTECT":
                r["recommendation"] = "PROTECT"
                _apply_protect_score_floor(r)
            r["above_studio_avg"] = r["recommendation"] == "PROTECT"
            r["studio_avg_fill"] = 0.0

        studio_avg_fill = {}
        for loc in locations:
            loc_recs = [r for r in group_records if r["location"] == loc]
            if loc_recs:
                studio_avg_fill[loc] = _weighted_records_avg(loc_recs, "avg_fill_rate")
                for r in loc_recs:
                    r["studio_avg_fill"] = round(studio_avg_fill[loc], 4)

        group_by_uid1 = {r["unique_id_1"]: r for r in group_records}
        trainer_records = trainer_agg.to_dict("records")
        _normalize_within_location(trainer_records, "avg_attendance", "avg_attendance_norm", locations)
        _normalize_within_location(trainer_records, "revenue_per_session", "revenue_norm", locations)
        _normalize_within_location(trainer_records, "session_count", "sessions_norm", locations)
        for tr in trainer_records:
            parent = group_by_uid1.get(str(tr[COL_UID1]), {})
            tr["location"] = tr[COL_LOCATION]
            tr["class"] = tr[COL_CLASS]
            tr["trainer"] = tr[COL_TRAINER]
            tr["day_name"] = str(tr[COL_DAY]).strip()
            tr["day"] = day_name_map.get(tr["day_name"], -1)
            tr["time"] = tr["time_clean"]
            tr["unique_id_1"] = str(tr[COL_UID1])
            tr["unique_id_2"] = str(tr[COL_UID2])
            tr["avg_checkin"] = round(float(tr["avg_attendance"]), 2)
            tr["avg_fill_rate"] = round(float(tr["fill_rate"]), 4)
            tr["avg_revenue"] = round(float(tr["revenue_per_session"]), 2)
            tr["score"], tr["score_breakdown"] = _score_record({
                **parent,
                "avg_attendance": tr["avg_checkin"],
                "avg_fill_rate": tr["avg_fill_rate"],
                "avg_revenue": tr["avg_revenue"],
                "session_count": tr["session_count"],
                "avg_attendance_norm": tr.get("avg_attendance_norm", 0.0),
                "grouped_avg_attendance": tr["avg_checkin"],
                "grouped_avg_attendance_norm": tr.get("avg_attendance_norm", 0.0),
                "revenue_norm": tr.get("revenue_norm", 0.0),
                "sessions_norm": tr.get("sessions_norm", 0.0),
            })
            tr["base_score"] = tr["score_breakdown"]["base_score"]
            tr["recency_boost"] = tr["score_breakdown"]["recency_boost"]
            tr["recommendation"] = _recommendation(tr["score"], int(tr["session_count"]))
            if int(tr["session_count"]) < CONSIDER_SESSIONS:
                tr["recommendation"] = "DROP"
            strength_protect = _is_strength_lab_protected(tr["class"], tr["avg_fill_rate"], tr["session_count"])
            fam_mode = _class_family_mode(tr["class"])
            baseline = family_loc_baseline.get((tr["location"], fam_mode), 0.0)
            if fam_mode in {"strength", "powercycle"}:
                top_performer_protect = int(tr["session_count"]) >= 8 and float(tr["avg_fill_rate"]) > float(baseline)
            else:
                top_performer_protect = int(tr["session_count"]) >= 8 and float(tr["avg_checkin"]) > float(baseline)
            tr["protect_exact_combo"] = strength_protect or top_performer_protect
            tr["protect_class_time"] = strength_protect or top_performer_protect
            if (strength_protect or top_performer_protect) and tr["recommendation"] == "PROTECT":
                tr["recommendation"] = "PROTECT"
                _apply_protect_score_floor(tr)
            tr["slot_trust"] = min(1.0, max(0.0, (int(tr["session_count"]) - 5) / 15.0))
            tr["longevity"] = min(1.0, max(0.0, (int(tr["session_count"]) - 4) / 16.0))
            tr["blended_fill"] = tr["avg_fill_rate"]
            tr["blended_checkin"] = tr["avg_checkin"]
            tr["trainer_total_sessions"] = int(tr["session_count"])
            tr["trainer_avg_ci"] = tr["avg_checkin"]
            tr["studio_avg_fill"] = parent.get("studio_avg_fill", 0.0)
            tr["above_studio_avg"] = tr["recommendation"] == "PROTECT"
            tr["historic_detail"] = {}

        # Attach top trainers to each UniqueID1 slot group, ranked by UniqueID2 performance.
        trainers_by_uid1 = defaultdict(list)
        for tr in sorted(trainer_records, key=lambda x: -float(x.get("score", 0))):
            trainers_by_uid1[tr["unique_id_1"]].append({
                "unique_id_2": tr["unique_id_2"],
                "trainer": tr["trainer"],
                "score": tr["score"],
                "avg_attendance": tr["avg_checkin"],
                "avg_fill_rate": tr["avg_fill_rate"],
                "avg_revenue": tr["avg_revenue"],
                "session_count": int(tr["session_count"]),
            })

        # Session-level drill-down by UniqueID1/UniqueID2 when Agent 1 state exists.
        historic_uid1 = {}
        historic_uid2 = {}
        try:
            sessions_path = Path(STATE_DIR) / "01_sessions.json"
            if sessions_path.exists():
                with open(sessions_path) as _f:
                    _sess = json.load(_f)
                sdf = pd.DataFrame(_sess.get("sessions", []))
                if not sdf.empty:
                    def _sess_time(t):
                        s = str(t).strip()
                        m = re.match(r"^(\d{1,2}):(\d{2})", s)
                        return f"{int(m.group(1)):02d}:{m.group(2)}" if m else s
                    sdf["time_clean"] = sdf["Time"].apply(_sess_time)
                    if COL_UID1 not in sdf.columns:
                        sdf[COL_UID1] = (
                            sdf[COL_LOCATION].astype(str) + "|" + sdf[COL_CLASS].astype(str) + "|" +
                            sdf[COL_DAY].astype(str) + "|" + sdf["time_clean"].astype(str)
                        )
                    else:
                        sdf[COL_UID1] = sdf[COL_UID1].astype(str)
                    if COL_UID2 not in sdf.columns:
                        sdf[COL_UID2] = sdf[COL_UID1].astype(str) + "|" + sdf[COL_TRAINER].astype(str)
                    else:
                        sdf[COL_UID2] = sdf[COL_UID2].astype(str)
                    for target, cols in ((historic_uid1, [COL_UID1]), (historic_uid2, [COL_UID2])):
                        for key, g in sdf.groupby(cols[0], dropna=False):
                            cap = pd.to_numeric(g["Capacity"], errors="coerce").fillna(1).clip(lower=1)
                            checked = pd.to_numeric(g["CheckedIn"], errors="coerce").fillna(0)
                            revenue = pd.to_numeric(g.get("Revenue", 0), errors="coerce").fillna(0)
                            target[str(key)] = {
                                "session_rows": int(len(g)),
                                "avg_checked_in": round(float(checked.mean()), 2),
                                "avg_booked": round(float(pd.to_numeric(g.get("Booked", 0), errors="coerce").fillna(0).mean()), 2),
                                "avg_capacity": round(float(cap.mean()), 2),
                                "avg_fill_rate": round(float((checked / cap).clip(upper=1.0).mean()), 4),
                                "avg_revenue": round(float(revenue.mean()), 2),
                                "total_revenue": round(float(revenue.sum()), 2),
                                "avg_late_cancel_rate": round(float(pd.to_numeric(g.get("late_cancel_rate", 0), errors="coerce").fillna(0).mean()), 4),
                                "avg_no_show_rate": round(float(pd.to_numeric(g.get("no_show_rate", 0), errors="coerce").fillna(0).mean()), 4),
                                "individual_sessions": [
                                    {
                                        "date": str(row.get("Date", ""))[:10],
                                        "trainer": str(row.get(COL_TRAINER, "")),
                                        "class": str(row.get(COL_CLASS, "")),
                                        "location": str(row.get(COL_LOCATION, "")),
                                        "day": str(row.get(COL_DAY, "")),
                                        "time": _sess_time(row.get(COL_TIME, "")),
                                        "checked_in": float(pd.to_numeric(row.get("CheckedIn", 0), errors="coerce")),
                                        "booked": float(pd.to_numeric(row.get("Booked", 0), errors="coerce")),
                                        "capacity": float(pd.to_numeric(row.get("Capacity", 0), errors="coerce")),
                                        "fill_rate": round(float(pd.to_numeric(row.get("CheckedIn", 0), errors="coerce")) / max(float(pd.to_numeric(row.get("Capacity", 1), errors="coerce")), 1.0), 4),
                                        "revenue": float(pd.to_numeric(row.get("Revenue", 0), errors="coerce")),
                                        "late_cancelled": float(pd.to_numeric(row.get("LateCancelled", 0), errors="coerce")),
                                        "late_cancel_rate": float(pd.to_numeric(row.get("late_cancel_rate", 0), errors="coerce")),
                                        "no_show_rate": float(pd.to_numeric(row.get("no_show_rate", 0), errors="coerce")),
                                        "unique_id_1": str(row.get(COL_UID1, "")),
                                        "unique_id_2": str(row.get(COL_UID2, "")),
                                    }
                                    for _, row in g.sort_values("Date", ascending=False).iterrows()
                                ],
                            }
                    for key, g in sdf.groupby([COL_LOCATION, COL_CLASS, COL_DAY, "time_clean"], dropna=False):
                        composite = "|".join(str(part) for part in key)
                        if composite not in historic_uid1:
                            first_uid = str(g.iloc[0].get(COL_UID1, ""))
                            if first_uid in historic_uid1:
                                historic_uid1[composite] = historic_uid1[first_uid]
                    for key, g in sdf.groupby([COL_LOCATION, COL_CLASS, COL_DAY, "time_clean", COL_TRAINER], dropna=False):
                        composite = "|".join(str(part) for part in key)
                        if composite not in historic_uid2:
                            first_uid = str(g.iloc[0].get(COL_UID2, ""))
                            if first_uid in historic_uid2:
                                historic_uid2[composite] = historic_uid2[first_uid]
        except Exception:
            pass

        class_slot_ranking = []
        for tr in trainer_records:
            uid2_composite = "|".join([
                str(tr["location"]), str(tr["class"]), str(tr["day_name"]),
                str(tr["time"]), str(tr["trainer"])
            ])
            hist_d = historic_uid2.get(tr["unique_id_2"]) or historic_uid2.get(uid2_composite, {})
            class_slot_ranking.append({
                "unique_id_1": tr["unique_id_1"],
                "unique_id_2": tr["unique_id_2"],
                "location": tr["location"],
                "class": tr["class"],
                "trainer": tr["trainer"],
                "day": tr["day"],
                "day_name": tr["day_name"],
                "time": tr["time"],
                "session_count": int(tr["session_count"]),
                "avg_checkin": tr["avg_checkin"],
                "avg_fill_rate": tr["avg_fill_rate"],
                "avg_revenue": tr["avg_revenue"],
                "blended_fill": tr["blended_fill"],
                "blended_checkin": tr["blended_checkin"],
                "trainer_total_sessions": tr["trainer_total_sessions"],
                "trainer_avg_ci": tr["trainer_avg_ci"],
                "slot_trust": round(float(tr["slot_trust"]), 3),
                "longevity": round(float(tr["longevity"]), 3),
                "recency_boost": tr["recency_boost"],
                "base_score": tr["base_score"],
                "score": tr["score"],
                "score_breakdown": tr["score_breakdown"],
                "recommendation": tr["recommendation"],
                "studio_avg_fill": tr["studio_avg_fill"],
                "above_studio_avg": tr["above_studio_avg"],
                "historic_detail": hist_d,
                "empty_sessions": max(0, int(hist_d.get("session_rows", int(tr["session_count"]))) - int(sum(1 for _x in hist_d.get("individual_sessions", []) if float(_x.get("checked_in", 0)) > 0))),
                "protect_exact_combo": bool(tr.get("protect_exact_combo")),
                "protect_class_time": bool(tr.get("protect_class_time")),
            })
        class_slot_ranking.sort(key=lambda x: -x["score"])

        slot_group_ranking = []
        for r in group_records:
            uid1 = r["unique_id_1"]
            uid1_composite = "|".join([
                str(r["location"]), str(r["class"]), str(r["day_name"]), str(r["time"])
            ])
            hist_s = historic_uid1.get(uid1) or historic_uid1.get(uid1_composite, {})
            slot_group_ranking.append({
                **r,
                "avg_checkin": r["avg_attendance"],
                "avg_fill_rate": r["avg_fill_rate"],
                "blended_fill": r["avg_fill_rate"],
                "blended_checkin": r["avg_attendance"],
                "slot_trust": min(1.0, max(0.0, (int(r["session_count"]) - 5) / 15.0)),
                "longevity": min(1.0, max(0.0, (int(r["session_count"]) - 4) / 16.0)),
                "historic_detail": hist_s,
                "empty_sessions": max(0, int(hist_s.get("session_rows", int(r["session_count"]))) - int(sum(1 for _x in hist_s.get("individual_sessions", []) if float(_x.get("checked_in", 0)) > 0))),
                "top_trainers": trainers_by_uid1.get(uid1, [])[:10],
            })
        slot_group_ranking.sort(key=lambda x: -x["score"])

        trainer_metrics = []
        for (loc, trainer), grp in trainer_agg.groupby([COL_LOCATION, COL_TRAINER]):
            trainer_metrics.append({
                "location": loc,
                "trainer": trainer,
                "trainer_avg_checkin": round(float(grp["avg_attendance"].mean()), 2),
                "trainer_fill_rate": round(float(grp["fill_rate"].mean()), 4),
                "trainer_session_count": int(grp["session_count"].sum()),
            })
        trainer_metrics.sort(key=lambda x: (x["location"], -x["trainer_avg_checkin"]))

        class_metrics = []
        for (loc, cls), grp in pd.DataFrame(group_records).groupby(["location", "class"]):
            total_sessions = int(grp["session_count"].sum())
            if total_sessions > 0:
                avg_fill = float(np.average(grp["avg_fill_rate"], weights=grp["session_count"].clip(lower=0)))
                avg_checkin = float(np.average(grp["avg_attendance"], weights=grp["session_count"].clip(lower=0)))
            else:
                avg_fill = float(grp["avg_fill_rate"].mean())
                avg_checkin = float(grp["avg_attendance"].mean())
            class_metrics.append({
                "location": loc,
                "class": cls,
                "avg_fill_rate": round(avg_fill, 4),
                "avg_checkin": round(avg_checkin, 2),
                "session_count": total_sessions,
            })
        class_metrics.sort(key=lambda x: (x["location"], -x["avg_checkin"]))

        protect = sum(1 for r in slot_group_ranking if r["recommendation"] == "PROTECT")
        pinned = sum(1 for r in slot_group_ranking if r.get("pinned_slot"))
        include = sum(1 for r in slot_group_ranking if r["recommendation"] == "INCLUDE")
        consider = sum(1 for r in slot_group_ranking if r["recommendation"] == "CONSIDER")
        drop = sum(1 for r in slot_group_ranking if r["recommendation"] == "DROP")
        total = len(slot_group_ranking)

        for loc, avg in sorted(studio_avg_fill.items()):
            print(f"  Studio avg fill [{loc[:25]}]: {avg:.1%}")

        print(f"[Agent 3] Scorer complete — {total:,} UniqueID1 class slots scored")
        print(f"  PINNED: {pinned}  PROTECT: {protect} ({protect/total*100:.1f}%)  "
              f"INCLUDE: {include} ({include/total*100:.1f}%)  "
              f"CONSIDER: {consider} ({consider/total*100:.1f}%)  DROP: {drop} ({drop/total*100:.1f}%)")
        print("  Formula: default classes use attendance 75%, fill 15%, revenue 7%, sessions 3%")

        output = {
            "weights_used": self.weights,
            "scoring_formula": "default score = avg_attendance_norm*75 + avg_fill_rate*15 + revenue_norm*7 + sessions_norm*3; PowerCycle/Strength use separate fill-first formulas",
            "slot_group_ranking": slot_group_ranking,
            "class_slot_ranking": class_slot_ranking,
            "trainer_ranking": trainer_metrics,
            "trainer_metrics": trainer_metrics,
            "class_type_ranking": class_metrics,
            "class_metrics": class_metrics,
        }

        atomic_write_json(STATE_DIR / "03_scores.json", output)

        return output
