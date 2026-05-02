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

# Pre-aggregated trainer×class×location stats (same value for all rows in group)
# NOTE: CSV column names are misleading — actual contents documented below
COL_CLASSES = "Classes"              # total sessions this trainer ran this class at this location
COL_AVG_CI_INCL = "ClassAvgInclEmpty"  # avg checkin incl zero-attendance sessions (labelled correctly)
COL_FILL_RATE = "ClassAvgExclEmpty"  # ACTUAL fill rate as "XX.XX%" (mislabelled in CSV)
COL_REV_PER_SESSION = "FillRate"     # avg revenue per session INR (mislabelled as FillRate in CSV)

# Absolute recommendation thresholds (not percentile — prevents 38%==78% problem)
PROTECT_SCORE = 55          # Lowered from 65 — high data quality combos deserve protection
PROTECT_SESSIONS = 10
INCLUDE_SCORE = 38
INCLUDE_SESSIONS = 5
CONSIDER_SCORE = 18
CONSIDER_SESSIONS = 3
AUTO_PROTECT_MIN_SESSIONS = 8  # Min sessions to auto-protect above-studio-avg combos

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
        vals = [r[key] for r in records if r.get("location") == loc]
        if not vals:
            continue
        mn, mx = min(vals), max(vals)
        if mx == mn:
            for r in records:
                if r.get("location") == loc:
                    r[out_key] = 0.5
            continue
        rng = mx - mn
        for r in records:
            if r.get("location") == loc:
                r[out_key] = (r[key] - mn) / rng


def _recommendation(score: float, trainer_sessions: int) -> str:
    if score >= PROTECT_SCORE and trainer_sessions >= PROTECT_SESSIONS:
        return "PROTECT"
    if score >= INCLUDE_SCORE and trainer_sessions >= INCLUDE_SESSIONS:
        return "INCLUDE"
    if score >= CONSIDER_SCORE and trainer_sessions >= CONSIDER_SESSIONS:
        return "CONSIDER"
    return "DROP"


class ClassScorer:
    def __init__(self, weights: dict = None, csv_path: str = None):
        self.weights = weights or {
            "blended_fill": 0.40,
            "blended_checkin": 0.30,
            "longevity": 0.20,
            "rev_per_session": 0.10,
        }
        self.csv_path = csv_path or "Class Performance by Trainer.csv"

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
        df[COL_CLASSES] = pd.to_numeric(df[COL_CLASSES], errors="coerce").fillna(0).astype(int)
        df[COL_AVG_CI_INCL] = pd.to_numeric(df[COL_AVG_CI_INCL], errors="coerce").fillna(0)
        df["fill_row"] = (df[COL_CHECKIN] / df[COL_CAPACITY]).clip(upper=1.0)
        df["trainer_fill"] = df[COL_FILL_RATE].apply(_parse_pct).clip(upper=1.0)
        df["trainer_rev"] = pd.to_numeric(df[COL_REV_PER_SESSION], errors="coerce").fillna(0)

        # -----------------------------------------------------------------
        # 2. Group by (location, class, trainer, day, time) → slot-level stats
        # -----------------------------------------------------------------
        group_cols = [COL_LOCATION, COL_CLASS, COL_TRAINER, COL_DAY, "time_clean"]

        slot_agg = df.groupby(group_cols).agg(
            slot_sessions=("fill_row", "count"),
            slot_checkin=(COL_CHECKIN, "mean"),
            slot_fill=("fill_row", "mean"),
            slot_revenue=(COL_REVENUE, "mean"),
            # Take first value for pre-aggregated trainer stats (same for all rows in group)
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
        #     Combos whose blended_fill > studio_avg AND >= AUTO_PROTECT_MIN_SESSIONS
        #     are automatically elevated to PROTECT regardless of score
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
                and int(r.get("trainer_sessions", 0)) >= AUTO_PROTECT_MIN_SESSIONS
            )

        # -----------------------------------------------------------------
        # 5b. Recency boost — load raw sessions to compute recent vs all-time fill
        #     Slots that perform better in the last 8 weeks get up to +8 points
        # -----------------------------------------------------------------
        recency_boost_map: dict = {}
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
        except Exception:
            pass

        # -----------------------------------------------------------------
        # 6. Composite score (0–100)
        # -----------------------------------------------------------------
        w = self.weights
        for r in records:
            base_score = (
                r.get("blended_fill_norm", 0.0) * w["blended_fill"] * 100
                + r.get("blended_checkin_norm", 0.0) * w["blended_checkin"] * 100
                + r.get("longevity", 0.0) * w["longevity"] * 100
                + r.get("blended_rev_norm", 0.0) * w["rev_per_session"] * 100
            )
            # Apply recency boost (can be negative for declining slots)
            _key = (r.get(COL_LOCATION), r.get(COL_CLASS), r.get(COL_TRAINER),
                    r.get(COL_DAY), r.get("time_clean"))
            boost = recency_boost_map.get(_key, 0.0)
            r["recency_boost"] = boost
            r["base_score"] = round(base_score, 2)
            r["score"] = round(float(np.clip(base_score + boost, 0, 100)), 2)

        # -----------------------------------------------------------------
        # 7. Absolute recommendation thresholds + above-studio-avg auto-protect
        # -----------------------------------------------------------------
        for r in records:
            rec = _recommendation(r["score"], r["trainer_sessions"])
            # Auto-elevate: above studio avg fill with enough history → always PROTECT
            if rec != "PROTECT" and r.get("above_studio_avg"):
                rec = "PROTECT"
            r["recommendation"] = rec

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
                "recommendation": r["recommendation"],
                "studio_avg_fill": r.get("studio_avg_fill", 0.0),
                "above_studio_avg": r.get("above_studio_avg", False),
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
            "class_slot_ranking": class_slot_ranking,
            "trainer_ranking": trainer_metrics,
            "trainer_metrics": trainer_metrics,
            "class_type_ranking": class_metrics,
            "class_metrics": class_metrics,
        }

        with open(STATE_DIR / "03_scores.json", "w") as f:
            json.dump(output, f)

        return output
