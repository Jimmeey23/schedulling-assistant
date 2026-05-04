"""
Agent 3 — Class Scorer (CSV-based, trust-weighted)
Reads 'Class Performance by Trainer.csv' directly.
Scores by blending slot-level stats with trainer-level aggregates (Bayesian trust).
Uses absolute thresholds — fixes the normalization problem where 38% and 78% fill both score INCLUDE.
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

PINNED_SCORE = 85
PINNED_SESSIONS = 10
SCORING_WEIGHTS = {
    "avg_attendance": 0.25,
    "capacity_fill": 0.55,
    "revenue": 0.15,
    "sessions": 0.05,
}
EXCLUDED_CLASS_KEYWORDS = (
    "hosted class",
    "pre/post natal",
    "pre post natal",
    "foundations",
    "sweat in 30",
    "unknown class",
)

PERMITTED_LOCATIONS = {
    "Kwality House, Kemps Corner",
    "Supreme HQ, Bandra",
    "Kenkere House",
}


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


class ClassScorer:
    def __init__(self, weights: dict = None, csv_path: str = None):
        self.weights = weights or SCORING_WEIGHTS
        self.csv_path = csv_path or "Class Performance by Trainer.csv"

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
        print("[Agent 3] Scorer starting (CSV trust-weighted mode)...")

        # -----------------------------------------------------------------
        # 1. Load CSV
        # -----------------------------------------------------------------
        csv_file = Path(self.csv_path)
        if not csv_file.exists():
            raise FileNotFoundError(
                f"CSV not found: {self.csv_path}. "
                "Pass --csv or place file in project root."
            )

        df = pd.read_csv(csv_file, low_memory=False)
        print(f"  Loaded {len(df):,} rows from {self.csv_path}")

        # Filter to permanent locations only
        df = df[df[COL_LOCATION].isin(PERMITTED_LOCATIONS)].copy()
        print(f"  After location filter: {len(df):,} rows")

        # Parse time to HH:MM
        def _clean_time(t):
            s = str(t).strip()
            m = re.match(r"^(\d{1,2}):(\d{2})", s)
            if m:
                return f"{int(m.group(1)):02d}:{m.group(2)}"
            return s

        df["time_clean"] = df[COL_TIME].apply(_clean_time)

        # Numeric columns
        df[COL_CHECKIN] = pd.to_numeric(df[COL_CHECKIN], errors="coerce").fillna(0)
        df[COL_CAPACITY] = pd.to_numeric(df[COL_CAPACITY], errors="coerce").fillna(1).clip(lower=1)
        df[COL_REVENUE] = pd.to_numeric(df[COL_REVENUE], errors="coerce").fillna(0)
        df["fill_row"] = (df[COL_CHECKIN] / df[COL_CAPACITY]).clip(upper=1.0)

        # Exclude non-schedulable classes and inactive trainers before any ranking.
        class_lower = df[COL_CLASS].astype(str).str.lower()
        excluded_mask = class_lower.apply(lambda x: any(k in x for k in EXCLUDED_CLASS_KEYWORDS))
        inactive = self._inactive_trainers()
        if inactive:
            excluded_mask = excluded_mask | df[COL_TRAINER].isin(inactive)
        df = df[~excluded_mask].copy()

        if COL_UID1 not in df.columns:
            df[COL_UID1] = (
                df[COL_LOCATION].astype(str) + "|" + df[COL_CLASS].astype(str) + "|" +
                df[COL_DAY].astype(str) + "|" + df["time_clean"].astype(str)
            )
        if COL_UID2 not in df.columns:
            df[COL_UID2] = df[COL_UID1].astype(str) + "|" + df[COL_TRAINER].astype(str)

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

        # -----------------------------------------------------------------
        # New scoring model:
        # 1) UniqueID1 ranks the class/location/day/time slot independent of trainer.
        # 2) UniqueID2 ranks trainers within that class slot for assignment.
        # -----------------------------------------------------------------
        trainer_group_cols = [COL_UID2, COL_UID1, COL_LOCATION, COL_CLASS, COL_TRAINER, COL_DAY, "time_clean"]
        trainer_agg = df.groupby(trainer_group_cols, dropna=False).agg(
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

        group_records = []
        for uid1, grp in trainer_agg.groupby(COL_UID1, dropna=False):
            first = grp.iloc[0]
            sessions = int(grp["session_count"].sum())
            group_records.append({
                "unique_id_1": str(uid1),
                "location": first[COL_LOCATION],
                "class": first[COL_CLASS],
                "day_name": str(first[COL_DAY]).strip(),
                "time": first["time_clean"],
                "session_count": sessions,
                "avg_attendance": round(_weighted_avg(grp, "avg_attendance"), 2),
                "avg_fill_rate": round(_weighted_avg(grp, "fill_rate"), 4),
                "avg_revenue": round(_weighted_avg(grp, "revenue_per_session"), 2),
                "total_revenue": round(float(grp["total_revenue"].sum()), 2),
                "trainer_count": int(grp[COL_TRAINER].nunique()),
            })

        locations = list(df[COL_LOCATION].unique())
        _normalize_within_location(group_records, "avg_attendance", "avg_attendance_norm", locations)
        _normalize_within_location(group_records, "avg_revenue", "revenue_norm", locations)
        _normalize_within_location(group_records, "session_count", "sessions_norm", locations)

        day_name_map = {
            "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
            "Friday": 4, "Saturday": 5, "Sunday": 6,
        }

        PRIME_AM = {"08:00","08:15","08:30","09:00","09:15","09:30","10:00","10:15","10:30","11:00","11:15","11:30"}
        PRIME_PM = {"17:30","17:45","18:00","18:15","18:30","19:00","19:15","19:30"}

        def _score_record(r):
            w = self.weights
            attendance_points = float(r.get("avg_attendance_norm", 0.0)) * w["avg_attendance"] * 100
            fill_points = float(r.get("avg_fill_rate", 0.0)) * w["capacity_fill"] * 100
            revenue_points = float(r.get("revenue_norm", 0.0)) * w["revenue"] * 100
            session_points = float(r.get("sessions_norm", 0.0)) * w["sessions"] * 100
            base = attendance_points + fill_points + revenue_points + session_points
            # Prime slot bonus: classes at peak times get a reliability boost
            t = str(r.get("time", "")).strip()[:5]
            prime_bonus = 7.0 if t in PRIME_AM or t in PRIME_PM else 0.0
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
                        "explanation": "+7 pts for peak AM (08:00-11:30) or peak PM (17:30-19:30) slots.",
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
                    "score = avg_fill_rate*55 + avg_attendance_norm*25 + "
                    "revenue_norm*15 + sessions_norm*5 + prime_bonus + volume_bonus + fill_abs_bonus"
                ),
                "components": [
                    {
                        "key": "avg_attendance",
                        "label": "Average Attendance",
                        "weight": w["avg_attendance"],
                        "raw_value": round(float(r.get("avg_attendance", 0.0)), 2),
                        "normalized_value": round(float(r.get("avg_attendance_norm", 0.0)), 4),
                        "points": round(attendance_points, 2),
                        "max_points": round(w["avg_attendance"] * 100, 2),
                        "explanation": "Normalized against other slots in the same studio.",
                    },
                    {
                        "key": "capacity_fill",
                        "label": "Capacity Fill Rate",
                        "weight": w["capacity_fill"],
                        "raw_value": round(float(r.get("avg_fill_rate", 0.0)), 4),
                        "normalized_value": round(float(r.get("avg_fill_rate", 0.0)), 4),
                        "points": round(fill_points, 2),
                        "max_points": round(w["capacity_fill"] * 100, 2),
                        "explanation": "Highest-weight component; direct average capacity fill rate.",
                    },
                    {
                        "key": "revenue",
                        "label": "Revenue Generated",
                        "weight": w["revenue"],
                        "raw_value": round(float(r.get("avg_revenue", 0.0)), 2),
                        "normalized_value": round(float(r.get("revenue_norm", 0.0)), 4),
                        "points": round(revenue_points, 2),
                        "max_points": round(w["revenue"] * 100, 2),
                        "explanation": "Revenue per session normalized against other slots in the same studio.",
                    },
                    {
                        "key": "sessions",
                        "label": "Number of Sessions",
                        "weight": w["sessions"],
                        "raw_value": int(r.get("session_count", 0)),
                        "normalized_value": round(float(r.get("sessions_norm", 0.0)), 4),
                        "points": round(session_points, 2),
                        "max_points": round(w["sessions"] * 100, 2),
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
            r["pinned_slot"] = False
            r["protect_class_time"] = False
            r["above_studio_avg"] = r["recommendation"] == "PROTECT"
            r["studio_avg_fill"] = 0.0

        studio_avg_fill = {}
        for loc in locations:
            loc_recs = [r for r in group_records if r["location"] == loc]
            if loc_recs:
                studio_avg_fill[loc] = sum(r["avg_fill_rate"] for r in loc_recs) / len(loc_recs)
                ranked = sorted([x for x in loc_recs if int(x["session_count"]) >= 5], key=lambda x: -x["score"])
                for rank, r in enumerate(ranked, start=1):
                    r["studio_avg_fill"] = round(studio_avg_fill[loc], 4)
                    if rank <= 2:
                        r["pinned_slot"] = True
                        r["recommendation"] = "PROTECT"
                        r["above_studio_avg"] = True
                    elif rank <= 10 and r["score"] >= INCLUDE_SCORE:
                        r["protect_class_time"] = True
                        r["recommendation"] = "PROTECT"
                        r["above_studio_avg"] = True
                for r in loc_recs:
                    r["studio_avg_fill"] = round(studio_avg_fill[loc], 4)

        group_by_uid1 = {r["unique_id_1"]: r for r in group_records}
        trainer_records = trainer_agg.to_dict("records")
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
            tr["avg_attendance_norm"] = parent.get("avg_attendance_norm", 0.0)
            tr["revenue_norm"] = parent.get("revenue_norm", 0.0)
            tr["sessions_norm"] = parent.get("sessions_norm", 0.0)
            tr["score"], tr["score_breakdown"] = _score_record({
                **parent,
                "avg_attendance": tr["avg_checkin"],
                "avg_fill_rate": tr["avg_fill_rate"],
                "avg_revenue": tr["avg_revenue"],
                "session_count": tr["session_count"],
            })
            tr["base_score"] = tr["score_breakdown"]["base_score"]
            tr["recency_boost"] = tr["score_breakdown"]["recency_boost"]
            tr["recommendation"] = _recommendation(tr["score"], int(tr["session_count"]))
            tr["protect_exact_combo"] = False
            tr["protect_class_time"] = False
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
                "historic_detail": historic_uid2.get(tr["unique_id_2"]) or historic_uid2.get(uid2_composite, {}),
                "protect_exact_combo": False,
                "protect_class_time": False,
            })
        class_slot_ranking.sort(key=lambda x: -x["score"])

        slot_group_ranking = []
        for r in group_records:
            uid1 = r["unique_id_1"]
            uid1_composite = "|".join([
                str(r["location"]), str(r["class"]), str(r["day_name"]), str(r["time"])
            ])
            slot_group_ranking.append({
                **r,
                "avg_checkin": r["avg_attendance"],
                "avg_fill_rate": r["avg_fill_rate"],
                "blended_fill": r["avg_fill_rate"],
                "blended_checkin": r["avg_attendance"],
                "slot_trust": min(1.0, max(0.0, (int(r["session_count"]) - 5) / 15.0)),
                "longevity": min(1.0, max(0.0, (int(r["session_count"]) - 4) / 16.0)),
                "historic_detail": historic_uid1.get(uid1) or historic_uid1.get(uid1_composite, {}),
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
            class_metrics.append({
                "location": loc,
                "class": cls,
                "avg_fill_rate": round(float(grp["avg_fill_rate"].mean()), 4),
                "avg_checkin": round(float(grp["avg_attendance"].mean()), 2),
                "session_count": int(grp["session_count"].sum()),
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
        print("  Formula: avg attendance 25%, fill rate 55%, revenue 15%, sessions 5%")

        output = {
            "weights_used": self.weights,
            "scoring_formula": "score = avg_fill_rate*55 + avg_attendance_norm*25 + revenue_norm*15 + sessions_norm*5",
            "slot_group_ranking": slot_group_ranking,
            "class_slot_ranking": class_slot_ranking,
            "trainer_ranking": trainer_metrics,
            "trainer_metrics": trainer_metrics,
            "class_type_ranking": class_metrics,
            "class_metrics": class_metrics,
        }

        atomic_write_json(STATE_DIR / "03_scores.json", output)

        return output

        # -----------------------------------------------------------------
        # 2. Group by (location, class, trainer, day, time) → slot-level stats
        # -----------------------------------------------------------------
        group_cols = [COL_LOCATION, COL_CLASS, COL_TRAINER, COL_DAY, "time_clean"]

        slot_agg = df.groupby(group_cols).agg(
            # Class Performance by Trainer.csv rows are already aggregated at
            # class×trainer×location×day×time. "Classes" is the historical
            # session count for this exact slot, not a row count.
            slot_sessions=(COL_CLASSES, "first"),
            slot_checkin=(COL_AVG_CI_INCL, "first"),
            slot_fill=("trainer_fill", "first"),
            slot_revenue=("trainer_rev", "first"),
            # Kept for the existing blended-score schema; in this aggregated
            # source these are the same exact-slot values.
            trainer_sessions=(COL_CLASSES, "first"),
            trainer_avg_ci=(COL_AVG_CI_INCL, "first"),
            trainer_fill=("trainer_fill", "first"),
            trainer_rev=("trainer_rev", "first"),
        ).reset_index()

        print(f"  {len(slot_agg):,} unique (location, class, trainer, day, time) combos")

        # -----------------------------------------------------------------
        # 3. Trust factor — Bayesian blend toward trainer average when slot is thin
        #    Improved: trust grows from 0→1 between 5 and 20 sessions (steeper ramp)
        # -----------------------------------------------------------------
        slot_agg["slot_trust"] = (
            ((slot_agg["slot_sessions"] - 5) / 15.0).clip(lower=0.0, upper=1.0)
        )

        slot_agg["blended_fill"] = (
            slot_agg["slot_trust"] * slot_agg["slot_fill"]
            + (1 - slot_agg["slot_trust"]) * slot_agg["trainer_fill"]
        ).clip(upper=1.0)
        slot_agg["blended_checkin"] = (
            slot_agg["slot_trust"] * slot_agg["slot_checkin"]
            + (1 - slot_agg["slot_trust"]) * slot_agg["trainer_avg_ci"]
        )

        # Revenue: blend slot revenue with trainer average
        slot_agg["blended_rev"] = (
            slot_agg["slot_trust"] * slot_agg["slot_revenue"]
            + (1 - slot_agg["slot_trust"]) * slot_agg["trainer_rev"]
        )

        # -----------------------------------------------------------------
        # 4. Longevity: 0 at ≤4 sessions, 1.0 at ≥20, linear in between
        # -----------------------------------------------------------------
        slot_agg["longevity"] = (
            (slot_agg["trainer_sessions"] - 4) / 16.0
        ).clip(lower=0.0, upper=1.0)

        # -----------------------------------------------------------------
        # 5. Normalize blended_fill, blended_checkin, blended_rev within location
        # -----------------------------------------------------------------
        records = slot_agg.to_dict("records")
        locations = list(slot_agg[COL_LOCATION].unique())

        _normalize_within_location(records, "blended_fill", "blended_fill_norm", locations)
        _normalize_within_location(records, "blended_checkin", "blended_checkin_norm", locations)
        _normalize_within_location(records, "blended_rev", "blended_rev_norm", locations)

        # -----------------------------------------------------------------
        # 5c. Studio average fill per location — basis for auto-protection
        #     Combos whose blended_fill > studio_avg AND have enough exact
        #     slot history are marked as above average. This is an analytical
        #     signal only; recommendation labels remain score/confidence gated.
        # -----------------------------------------------------------------
        studio_avg_fill: dict = {}
        for loc in locations:
            loc_recs = [r for r in records if r.get(COL_LOCATION) == loc]
            if loc_recs:
                studio_avg_fill[loc] = sum(r["blended_fill"] for r in loc_recs) / len(loc_recs)

        for r in records:
            loc = r.get(COL_LOCATION, "")
            avg = studio_avg_fill.get(loc, 0.0)
            r["studio_avg_fill"] = round(avg, 4)
            r["above_studio_avg"] = bool(
                r["blended_fill"] > avg
                and int(r.get("slot_sessions", 0)) >= AUTO_PROTECT_MIN_SESSIONS
            )

        # -----------------------------------------------------------------
        # 5b. Recency boost — load raw sessions to compute recent vs all-time fill
        #     Slots that perform better in the last 8 weeks get up to +8 points
        # -----------------------------------------------------------------
        recency_boost_map: dict = {}
        historic_detail_map: dict = {}
        try:
            import json as _json
            sessions_path = Path(STATE_DIR) / "01_sessions.json"
            if sessions_path.exists():
                with open(sessions_path) as _f:
                    _sess = _json.load(_f)
                import pandas as _pd2
                sdf = _pd2.DataFrame(_sess.get("sessions", []))
                if not sdf.empty:
                    sdf["Date"] = _pd2.to_datetime(sdf["Date"])
                    _max = sdf["Date"].max()
                    _cutoff = _max - _pd2.Timedelta(weeks=8)
                    sdf["fill_r"] = (sdf["CheckedIn"] / sdf["Capacity"].clip(lower=1)).clip(upper=1.0)
                    sdf["is_recent"] = sdf["Date"] >= _cutoff

                    def _clean_t(t):
                        s2 = str(t).strip()
                        import re as _re
                        m2 = _re.match(r"^(\d{1,2}):(\d{2})", s2)
                        return f"{int(m2.group(1)):02d}:{m2.group(2)}" if m2 else s2

                    sdf["time_clean"] = sdf["Time"].apply(_clean_t)
                    _gcols = [COL_LOCATION, COL_CLASS, COL_TRAINER, COL_DAY, "time_clean"]
                    for _gkey, _gdf in sdf.groupby(_gcols, dropna=False):
                        _all_fill = float(_gdf["fill_r"].mean())
                        _rec = _gdf[_gdf["is_recent"]]
                        if len(_rec) >= 2 and _all_fill > 0:
                            _rec_fill = float(_rec["fill_r"].mean())
                            _momentum = min(2.0, _rec_fill / _all_fill)
                            recency_boost_map[_gkey] = round((_momentum - 1.0) * 8.0, 2)
                        historic_detail_map[_gkey] = {
                            "session_rows": int(len(_gdf)),
                            "avg_checked_in": round(float(_gdf["CheckedIn"].mean()), 2),
                            "avg_booked": round(float(_gdf.get("Booked", _pd2.Series([0] * len(_gdf))).mean()), 2),
                            "avg_capacity": round(float(_gdf["Capacity"].mean()), 2),
                            "avg_fill_rate": round(_all_fill, 4),
                            "avg_revenue": round(float(_gdf.get("Revenue", _pd2.Series([0] * len(_gdf))).mean()), 2),
                            "total_revenue": round(float(_gdf.get("Revenue", _pd2.Series([0] * len(_gdf))).sum()), 2),
                            "avg_late_cancel_rate": round(float(_gdf.get("late_cancel_rate", _pd2.Series([0] * len(_gdf))).mean()), 4),
                            "avg_no_show_rate": round(float(_gdf.get("no_show_rate", _pd2.Series([0] * len(_gdf))).mean()), 4),
                        }
        except Exception:
            pass

        # -----------------------------------------------------------------
        # 6. Composite score (0–100)
        #    blended_fill used DIRECTLY (0–1 → 0–55 pts) so a 55% fill slot
        #    actually earns ~30 pts from fill alone — not crushed by within-
        #    location min-max that made 50% fill look like 5 pts.
        #    checkin / rev still normalised within-location (absolute values
        #    are not comparable across class types).
        # -----------------------------------------------------------------
        w = self.weights
        for r in records:
            fill_points = r.get("blended_fill", 0.0) * w["blended_fill"] * 100
            checkin_points = r.get("blended_checkin_norm", 0.0) * w["blended_checkin"] * 100
            longevity_points = r.get("longevity", 0.0) * w["longevity"] * 100
            revenue_points = r.get("blended_rev_norm", 0.0) * w["rev_per_session"] * 100
            base_score = fill_points + checkin_points + longevity_points + revenue_points
            # Apply recency boost (can be negative for declining slots)
            _key = (r.get(COL_LOCATION), r.get(COL_CLASS), r.get(COL_TRAINER),
                    r.get(COL_DAY), r.get("time_clean"))
            boost = recency_boost_map.get(_key, 0.0)
            r["recency_boost"] = boost
            r["historic_detail"] = historic_detail_map.get(_key, {})
            r["base_score"] = round(base_score, 2)
            r["score"] = round(float(np.clip(base_score + boost, 0, 100)), 2)
            r["score_breakdown"] = {
                "total_score": r["score"],
                "base_score": r["base_score"],
                "recency_boost": round(float(boost), 2),
                "components": [
                    {
                        "key": "blended_fill",
                        "label": "Blended Fill",
                        "weight": w["blended_fill"],
                        "raw_value": round(float(r.get("blended_fill", 0.0)), 4),
                        "normalized_value": round(float(r.get("blended_fill", 0.0)), 4),
                        "points": round(float(fill_points), 2),
                        "max_points": round(w["blended_fill"] * 100, 2),
                        "explanation": "Direct fill-rate contribution; a 60% blended fill earns 60% of this component.",
                    },
                    {
                        "key": "blended_checkin",
                        "label": "Avg Check-In",
                        "weight": w["blended_checkin"],
                        "raw_value": round(float(r.get("blended_checkin", 0.0)), 2),
                        "normalized_value": round(float(r.get("blended_checkin_norm", 0.0)), 4),
                        "points": round(float(checkin_points), 2),
                        "max_points": round(w["blended_checkin"] * 100, 2),
                        "explanation": "Check-ins normalized against other combinations in the same studio.",
                    },
                    {
                        "key": "longevity",
                        "label": "Evidence Depth",
                        "weight": w["longevity"],
                        "raw_value": int(r.get("trainer_sessions", 0)),
                        "normalized_value": round(float(r.get("longevity", 0.0)), 4),
                        "points": round(float(longevity_points), 2),
                        "max_points": round(w["longevity"] * 100, 2),
                        "explanation": "Historical depth: full credit at 20+ sessions, low credit with thin history.",
                    },
                    {
                        "key": "revenue",
                        "label": "Revenue",
                        "weight": w["rev_per_session"],
                        "raw_value": round(float(r.get("blended_rev", 0.0)), 2),
                        "normalized_value": round(float(r.get("blended_rev_norm", 0.0)), 4),
                        "points": round(float(revenue_points), 2),
                        "max_points": round(w["rev_per_session"] * 100, 2),
                        "explanation": "Revenue per session normalized against other combinations in the same studio.",
                    },
                ],
            }

        # -----------------------------------------------------------------
        # 7. Absolute recommendation thresholds
        # -----------------------------------------------------------------
        for r in records:
            r["recommendation"] = _recommendation(r["score"], int(r.get("slot_sessions", 0)))

        # -----------------------------------------------------------------
        # 8. Normalise key names to match downstream schema
        # -----------------------------------------------------------------
        day_name_map = {
            "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
            "Friday": 4, "Saturday": 5, "Sunday": 6,
        }

        class_slot_ranking = []
        for r in records:
            day_str = str(r.get(COL_DAY, "")).strip()
            day_int = day_name_map.get(day_str, -1)
            class_slot_ranking.append({
                "location": r[COL_LOCATION],
                "class": r[COL_CLASS],
                "trainer": r[COL_TRAINER],
                "day": day_int,
                "day_name": day_str,
                "time": r["time_clean"],
                "session_count": int(r["slot_sessions"]),
                "avg_checkin": round(float(r["slot_checkin"]), 2),
                "avg_fill_rate": round(float(r["slot_fill"]), 4),
                "avg_revenue": round(float(r["slot_revenue"]), 2),
                "blended_fill": round(float(r["blended_fill"]), 4),
                "blended_checkin": round(float(r["blended_checkin"]), 2),
                "trainer_total_sessions": int(r["trainer_sessions"]),
                "trainer_avg_ci": round(float(r["trainer_avg_ci"]), 2),
                "slot_trust": round(float(r["slot_trust"]), 3),
                "longevity": round(float(r["longevity"]), 3),
                "recency_boost": round(float(r.get("recency_boost", 0.0)), 2),
                "base_score": r["base_score"],
                "score": r["score"],
                "score_breakdown": r["score_breakdown"],
                "recommendation": r["recommendation"],
                "studio_avg_fill": r.get("studio_avg_fill", 0.0),
                "above_studio_avg": r.get("above_studio_avg", False),
                "historic_detail": r.get("historic_detail", {}),
            })

        class_slot_ranking.sort(key=lambda x: -x["score"])

        # -----------------------------------------------------------------
        # 9. Trainer and class-type aggregates (for downstream agents)
        # -----------------------------------------------------------------
        trainer_metrics = []
        for (loc, trainer), grp in slot_agg.groupby([COL_LOCATION, COL_TRAINER]):
            trainer_metrics.append({
                "location": loc,
                "trainer": trainer,
                "trainer_avg_checkin": round(float(grp["slot_checkin"].mean()), 2),
                "trainer_fill_rate": round(float(grp["slot_fill"].mean()), 4),
                "trainer_session_count": int(grp["slot_sessions"].sum()),
            })
        trainer_metrics.sort(key=lambda x: (x["location"], -x["trainer_avg_checkin"]))

        class_metrics = []
        for (loc, cls), grp in slot_agg.groupby([COL_LOCATION, COL_CLASS]):
            class_metrics.append({
                "location": loc,
                "class": cls,
                "avg_fill_rate": round(float(grp["slot_fill"].mean()), 4),
                "avg_checkin": round(float(grp["slot_checkin"].mean()), 2),
                "session_count": int(grp["slot_sessions"].sum()),
            })
        class_metrics.sort(key=lambda x: (x["location"], -x["avg_fill_rate"]))

        # -----------------------------------------------------------------
        # 10. Print distribution summary
        # -----------------------------------------------------------------
        protect = sum(1 for r in class_slot_ranking if r["recommendation"] == "PROTECT")
        include = sum(1 for r in class_slot_ranking if r["recommendation"] == "INCLUDE")
        consider = sum(1 for r in class_slot_ranking if r["recommendation"] == "CONSIDER")
        drop = sum(1 for r in class_slot_ranking if r["recommendation"] == "DROP")
        auto_prot = sum(1 for r in class_slot_ranking if r.get("above_studio_avg"))
        total = len(class_slot_ranking)

        for loc, avg in sorted(studio_avg_fill.items()):
            print(f"  Studio avg fill [{loc[:25]}]: {avg:.1%}")

        print(f"[Agent 3] Scorer complete — {total:,} unique combos scored")
        print(f"  PROTECT: {protect} ({protect/total*100:.1f}%)  "
              f"INCLUDE: {include} ({include/total*100:.1f}%)  "
              f"CONSIDER: {consider} ({consider/total*100:.1f}%)  "
              f"DROP: {drop} ({drop/total*100:.1f}%)")
        print(f"  Auto-protected (above studio avg): {auto_prot}")
        print(f"  Thresholds: PROTECT≥{PROTECT_SCORE}+{PROTECT_SESSIONS}sess  "
              f"INCLUDE≥{INCLUDE_SCORE}+{INCLUDE_SESSIONS}sess  "
              f"CONSIDER≥{CONSIDER_SCORE}+{CONSIDER_SESSIONS}sess")

        output = {
            "weights_used": self.weights,
            "scoring_formula": "score = blended_fill*55 + blended_checkin_norm*20 + revenue_norm*15 + longevity*10 + recency_boost",
            "class_slot_ranking": class_slot_ranking,
            "trainer_ranking": trainer_metrics,
            "trainer_metrics": trainer_metrics,
            "class_type_ranking": class_metrics,
            "class_metrics": class_metrics,
        }

        atomic_write_json(STATE_DIR / "03_scores.json", output)

        return output
