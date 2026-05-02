import json
import pandas as pd
import numpy as np
from pathlib import Path

STATE_DIR = Path("state")
VALID_LOCATIONS = [
    "Kwality House, Kemps Corner",
    "Supreme HQ, Bandra",
    "Kenkere House",
]


def time_band(time_str: str) -> str:
    try:
        h = int(str(time_str)[:2])
    except (ValueError, TypeError):
        return "unknown"
    if 7 <= h <= 9:
        return "morning"
    elif 10 <= h <= 12:
        return "midday"
    elif 13 <= h <= 16:
        return "afternoon"
    else:
        return "evening"


class DataIngestor:
    def __init__(self, csv_path: Path):
        self.csv_path = Path(csv_path)

    def run(self) -> dict:
        print("[Agent 1] Ingestor starting...")
        df = pd.read_csv(self.csv_path)

        # Filter to valid locations
        df = df[df["Location"].isin(VALID_LOCATIONS)].copy()

        # Parse date
        df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
        df = df.dropna(subset=["Date"])

        # Normalize time to HH:MM
        df["Time"] = df["Time"].astype(str).str.strip().str[:5]

        # Normalize whitespace in text columns (collapse double-spaces, strip)
        for col in ["Trainer", "Class", "Location"]:
            if col in df.columns:
                df[col] = df[col].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()

        # Ensure numeric columns
        for col in ["CheckedIn", "Capacity", "Booked", "LateCancelled", "Revenue"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        # Derived columns
        df["fill_rate"] = np.where(
            df["Capacity"] > 0,
            (df["CheckedIn"] / df["Capacity"]).clip(upper=1.0),
            0.0,
        )
        df["no_show_rate"] = np.where(
            df["Booked"] > 0,
            (df["Booked"] - df["CheckedIn"]) / df["Booked"],
            0.0,
        )
        df["late_cancel_rate"] = np.where(
            df["Booked"] > 0,
            df["LateCancelled"] / df["Booked"],
            0.0,
        )
        df["revenue_per_seat"] = np.where(
            df["CheckedIn"] > 0,
            df["Revenue"] / df["CheckedIn"],
            0.0,
        )
        df["day_of_week"] = df["Date"].dt.dayofweek  # Monday=0
        df["time_band"] = df["Time"].apply(time_band)

        # Drop rows with missing critical fields
        df = df.dropna(subset=["Location", "Class", "Trainer", "Time"])

        total = len(df)
        date_min = df["Date"].min().strftime("%Y-%m-%d")
        date_max = df["Date"].max().strftime("%Y-%m-%d")

        records = json.loads(df.to_json(orient="records", date_format="iso"))

        output = {
            "locations": VALID_LOCATIONS,
            "total_sessions": total,
            "date_range": {"min": date_min, "max": date_max},
            "sessions": records,
        }

        STATE_DIR.mkdir(exist_ok=True)
        out_path = STATE_DIR / "01_sessions.json"
        with open(out_path, "w") as f:
            json.dump(output, f, default=str)

        print(
            f"[Agent 1] Ingestor complete — {total:,} sessions across {len(VALID_LOCATIONS)} locations"
        )
        return output
