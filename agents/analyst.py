import json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import timedelta

STATE_DIR = Path("state")


def linear_slope(series):
    if len(series) < 2:
        return 0.0
    x = np.arange(len(series), dtype=float)
    y = np.array(series, dtype=float)
    if np.std(x) == 0:
        return 0.0
    return float(np.polyfit(x, y, 1)[0])


class PerformanceAnalyst:
    def run(self) -> dict:
        print("[Agent 2] Analyst starting...")
        with open(STATE_DIR / "01_sessions.json") as f:
            data = json.load(f)

        sessions = data["sessions"]
        df = pd.DataFrame(sessions)
        df["Date"] = pd.to_datetime(df["Date"])

        max_date = df["Date"].max()
        cutoff_12w = max_date - timedelta(weeks=12)
        cutoff_8w  = max_date - timedelta(weeks=8)

        # Recency weight: sessions in the last 8 weeks count 3×
        df["recency_weight"] = np.where(df["Date"] >= cutoff_8w, 3.0, 1.0)

        # --- class_trainer_slot_metrics ---
        group_cols = ["Location", "Class", "Trainer", "day_of_week", "Time"]
        metrics = []
        for key, grp in df.groupby(group_cols, dropna=False):
            loc, cls, trainer, dow, time = key
            grp_sorted = grp.sort_values("Date")
            slope = linear_slope(grp_sorted["fill_rate"].tolist())
            revenue_slope = linear_slope(grp_sorted["Revenue"].tolist())
            last_12w = int((grp["Date"] >= cutoff_12w).sum())
            last_8w  = int((grp["Date"] >= cutoff_8w).sum())

            # Recency-weighted fill rate and checkin
            weights = grp["recency_weight"].values
            total_w = weights.sum()
            if total_w > 0:
                recency_weighted_fill   = float(np.average(grp["fill_rate"].values, weights=weights))
                recency_weighted_checkin = float(np.average(grp["CheckedIn"].values, weights=weights))
            else:
                recency_weighted_fill   = float(grp["fill_rate"].mean())
                recency_weighted_checkin = float(grp["CheckedIn"].mean())

            # Recency momentum: ratio of recent (8w) fill to all-time fill (capped 0–2)
            all_time_fill = float(grp["fill_rate"].mean())
            recent_grp = grp[grp["Date"] >= cutoff_8w]
            if last_8w >= 2 and all_time_fill > 0:
                recent_fill = float(recent_grp["fill_rate"].mean())
                recency_momentum = round(min(2.0, recent_fill / all_time_fill), 3)
            else:
                recency_momentum = 1.0

            # consistency_score: stddev of fill_rate (lower = more consistent)
            fill_vals = grp["fill_rate"].tolist()
            if len(fill_vals) >= 2:
                consistency_score = float(np.std(fill_vals))
            else:
                consistency_score = 0.0

            # peak_day stored as current day; enriched post-loop via cross-group data
            peak_day = int(dow)
            best_time_band = str(grp["time_band"].mode().iloc[0]) if len(grp) > 0 else "morning"

            metrics.append(
                {
                    "location": loc,
                    "class": cls,
                    "trainer": trainer,
                    "day": int(dow),
                    "time": time,
                    "session_count": int(len(grp)),
                    "avg_checkin": float(grp["CheckedIn"].mean()),
                    "avg_fill_rate": float(grp["fill_rate"].mean()),
                    "avg_fill_rate_recency": round(recency_weighted_fill, 4),
                    "avg_checkin_recency": round(recency_weighted_checkin, 2),
                    "recency_momentum": recency_momentum,
                    "avg_revenue": float(grp["Revenue"].mean()),
                    "avg_revenue_per_seat": float(grp["revenue_per_seat"].mean()),
                    "avg_late_cancel_rate": float(grp["late_cancel_rate"].mean()),
                    "avg_no_show_rate": float(grp["no_show_rate"].mean()),
                    "total_revenue": float(grp["Revenue"].sum()),
                    "trend_fill_rate": slope,
                    "revenue_trend": revenue_slope,
                    "sessions_last_12_weeks": last_12w,
                    "sessions_last_8_weeks": last_8w,
                    "consistency_score": round(consistency_score, 4),
                    "peak_day": peak_day,
                    "best_time_band": best_time_band,
                }
            )

        # --- Enrich peak_day and best_time_band using cross-day data ---
        # For each (location, class, trainer), find which day_of_week and
        # which time_band has the highest avg_checkin.
        lct_day_checkin: dict = {}   # (loc, cls, trainer, day) -> [checkins]
        lct_band_checkin: dict = {}  # (loc, cls, trainer, band) -> [checkins]
        for key, grp in df.groupby(["Location", "Class", "Trainer"], dropna=False):
            loc, cls, trainer = key
            for dow_val, day_grp in grp.groupby("day_of_week", dropna=False):
                k = (loc, cls, trainer, int(dow_val))
                lct_day_checkin[k] = float(day_grp["CheckedIn"].mean())
            for band_val, band_grp in grp.groupby("time_band", dropna=False):
                k = (loc, cls, trainer, str(band_val))
                lct_band_checkin[k] = float(band_grp["CheckedIn"].mean())

        # Build lookup: (loc, cls, trainer) -> best day and best band
        lct_best_day: dict = {}
        lct_best_band: dict = {}
        lct_keys = set((r["location"], r["class"], r["trainer"]) for r in metrics)
        for (loc, cls, trainer) in lct_keys:
            # best day
            day_vals = {
                d: lct_day_checkin[(loc, cls, trainer, d)]
                for d in range(7)
                if (loc, cls, trainer, d) in lct_day_checkin
            }
            lct_best_day[(loc, cls, trainer)] = (
                max(day_vals, key=day_vals.get) if day_vals else 0
            )
            # best band
            band_vals = {
                b: lct_band_checkin[(loc, cls, trainer, b)]
                for b in ["morning", "midday", "afternoon", "evening"]
                if (loc, cls, trainer, b) in lct_band_checkin
            }
            lct_best_band[(loc, cls, trainer)] = (
                max(band_vals, key=band_vals.get) if band_vals else "morning"
            )

        for r in metrics:
            k = (r["location"], r["class"], r["trainer"])
            r["peak_day"] = lct_best_day.get(k, r["peak_day"])
            r["best_time_band"] = lct_best_band.get(k, r["best_time_band"])

        # --- class_metrics (by location + class) ---
        class_metrics = []
        for (loc, cls), grp in df.groupby(["Location", "Class"], dropna=False):
            class_metrics.append(
                {
                    "location": loc,
                    "class": cls,
                    "avg_fill_rate": float(grp["fill_rate"].mean()),
                    "avg_checkin": float(grp["CheckedIn"].mean()),
                    "avg_revenue": float(grp["Revenue"].mean()),
                    "total_revenue": float(grp["Revenue"].sum()),
                    "session_count": int(len(grp)),
                }
            )

        # --- trainer_metrics ---
        trainer_metrics = []
        for (loc, trainer), grp in df.groupby(["Location", "Trainer"], dropna=False):
            trainer_metrics.append(
                {
                    "location": loc,
                    "trainer": trainer,
                    "trainer_avg_checkin": float(grp["CheckedIn"].mean()),
                    "trainer_fill_rate": float(grp["fill_rate"].mean()),
                    "trainer_session_count": int(len(grp)),
                }
            )

        # --- slot_metrics ---
        slot_metrics = []
        for (loc, time), grp in df.groupby(["Location", "Time"], dropna=False):
            slot_metrics.append(
                {
                    "location": loc,
                    "time": time,
                    "avg_fill_rate": float(grp["fill_rate"].mean()),
                    "avg_checkin": float(grp["CheckedIn"].mean()),
                    "session_count": int(len(grp)),
                }
            )

        # --- slot_availability: viable (location, time) pairs with enough history ---
        # A slot is viable if session_count >= 10 AND fill_rate >= 0.25
        # (per CLAUDE.md spec — slightly looser floor than the old internal threshold)
        MIN_SESSIONS = 10
        MIN_FILL = 0.25
        slot_availability: dict = {}
        for s in slot_metrics:
            loc = s["location"]
            if loc not in slot_availability:
                slot_availability[loc] = []
            slot_availability[loc].append({
                "time": s["time"],
                "session_count": s["session_count"],
                "avg_fill_rate": s["avg_fill_rate"],
                "avg_checkin": s["avg_checkin"],
                "viable": s["session_count"] >= MIN_SESSIONS and s["avg_fill_rate"] >= MIN_FILL,
            })
        # Sort each location's slots by time
        for loc in slot_availability:
            slot_availability[loc].sort(key=lambda x: x["time"])

        # --- day_band_metrics: fill rate and volume by (location, day, time_band) ---
        day_band_metrics = []
        try:
            for (loc, dow, band), grp in df.groupby(
                ["Location", "day_of_week", "time_band"], dropna=False
            ):
                day_band_metrics.append(
                    {
                        "location": loc,
                        "day": int(dow),
                        "band": str(band),
                        "avg_fill_rate": float(grp["fill_rate"].mean()),
                        "avg_checkin": float(grp["CheckedIn"].mean()),
                        "session_count": int(len(grp)),
                    }
                )
        except Exception as e:
            print(f"[Agent 2] Warning: could not compute day_band_metrics: {e}")

        output = {
            "class_trainer_slot_metrics": metrics,
            "class_metrics": class_metrics,
            "trainer_metrics": trainer_metrics,
            "slot_metrics": slot_metrics,
            "slot_availability": slot_availability,
            "day_band_metrics": day_band_metrics,
        }

        with open(STATE_DIR / "02_metrics.json", "w") as f:
            json.dump(output, f)

        viable_count = sum(
            1 for loc_slots in slot_availability.values() for s in loc_slots if s["viable"]
        )
        day_band_count = len(day_band_metrics)
        print(
            f"[Agent 2] Analyst complete — recency-weighted metrics, slot viability, day/band breakdowns"
        )
        print(
            f"  {len(metrics):,} class×trainer×slot combos analysed, "
            f"{viable_count} viable time slots across all locations, "
            f"{day_band_count} day×band breakdown entries, "
            f"8-week recency window applied (3× weight)"
        )
        return output
