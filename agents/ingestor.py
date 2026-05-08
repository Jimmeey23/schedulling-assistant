import csv
import json
import os
import pandas as pd
import numpy as np
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=False)

STATE_DIR = Path("state")
VALID_LOCATIONS = [
    "Kwality House, Kemps Corner",
    "Supreme HQ, Bandra",
    "Kenkere House",
    "Copper & Cloves",
]


def copper_class_name(row) -> str:
    session = str(row.get("SessionName", "") or "").lower()
    class_name = str(row.get("Class", "") or "")
    source = f"{session} {class_name.lower()}"
    if "fit" in source:
        return "Copper + Cloves FIT"
    if "mat 57" in source or "mat57" in source:
        return "Copper + Cloves Mat 57"
    return "Copper + Cloves Barre 57"


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
    def __init__(
        self,
        csv_path: Path = None,
        *,
        client_id: str = None,
        client_secret: str = None,
        refresh_token: str = None,
    ):
        self.csv_path = Path(csv_path) if csv_path else None
        self.client_id = client_id or os.environ.get("GSHEETS_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("GSHEETS_CLIENT_SECRET")
        self.refresh_token = refresh_token or os.environ.get("GSHEETS_REFRESH_TOKEN")

    def _use_sheets(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    def _read_sessions_file(self) -> pd.DataFrame:
        if self._use_sheets():
            from utils.google_sheets import read_sheet
            print("[Agent 1] Fetching data from Google Sheets (Sessions)...")
            return read_sheet("Sessions", self.client_id, self.client_secret, self.refresh_token)

        sample = self.csv_path.read_text(encoding="utf-8", errors="replace")[:8192]
        delimiter = "\t"
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            delimiter = dialect.delimiter
        except csv.Error:
            first_line = sample.splitlines()[0] if sample else ""
            if "\t" in first_line:
                delimiter = "\t"
            elif ";" in first_line:
                delimiter = ";"
            elif "|" in first_line:
                delimiter = "|"
            else:
                delimiter = ","

        return pd.read_csv(
            self.csv_path,
            sep=delimiter,
            engine="python",
            on_bad_lines="warn",
        )

    def run(self) -> dict:
        print("[Agent 1] Ingestor starting...")
        df = self._read_sessions_file()

        # Parse date
        df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
        df = df.dropna(subset=["Date"])

        # Normalize time to HH:MM
        df["Time"] = df["Time"].astype(str).str.strip().str[:5]

        # Normalize whitespace in text columns (collapse double-spaces, strip)
        for col in ["Trainer", "Class", "Location"]:
            if col in df.columns:
                df[col] = df[col].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()

        # Copper & Cloves is scheduled as a Bengaluru extension using historic
        # Pop-up rows whose class name identifies the Copper partnership.
        session_names = df["SessionName"] if "SessionName" in df.columns else ""
        copper_mask = (
            df["Location"].astype(str).str.lower().eq("pop-up")
            & pd.Series(session_names, index=df.index).astype(str).str.contains("copper", case=False, na=False)
        )
        df.loc[copper_mask, "Location"] = "Copper & Cloves"
        df.loc[copper_mask, "Class"] = df.loc[copper_mask].apply(copper_class_name, axis=1)

        # Filter to valid scheduling locations after derived-location mapping.
        df = df[df["Location"].isin(VALID_LOCATIONS)].copy()

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
