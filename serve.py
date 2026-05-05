"""
serve.py — Local HTTP server for Studio Scheduler web interface.
Serves the generated schedule web UI and exposes API endpoints for rule toggling.

Usage:
    python3 serve.py --week 2026-05-04 --port 8080
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time as _time
import uuid
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

from finalise_schedule import finalise_schedule_document
from rule_config import build_rules_catalog, load_rules_config, update_rules_config

PROJECT_ROOT = Path(__file__).parent
WEB_DIR = PROJECT_ROOT / "web"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
CONFIG_PATH = PROJECT_ROOT / "config" / "rules_config.json"
SCHEDULE_CONFIG_PATH = PROJECT_ROOT / "config" / "schedule_config.json"
TRAINER_PROFILES_PATH = PROJECT_ROOT / "rules" / "trainer_profiles.json"
MUMBAI_LOCATIONS = {"Kwality House, Kemps Corner", "Supreme HQ, Bandra", "Courtside"}
BENGALURU_LOCATIONS = {"Kenkere House", "Copper & Cloves"}
MAIN_STUDIOS = {"Kwality House, Kemps Corner", "Supreme HQ, Bandra", "Kenkere House"}
DERIVED_STUDIOS = {"Courtside", "Copper & Cloves"}


def load_env_file(path: Path = PROJECT_ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()

# Pipeline process state
_pipeline_state = {
    "running": False,
    "status": "idle",
    "pid": None,
    "started": None,
    "message": "Idle",
}
_run_counter = 0


def _same_schedule_slot(row, slot):
    keys = ("location", "date", "day_of_week", "time", "class_name", "room", "trainer_1")
    return all((row.get(k) or "") == (slot.get(k) or "") for k in keys)


def _slot_minutes(value):
    if not value:
        return 0
    h, m = str(value).split(":")[:2]
    return int(h) * 60 + int(m)


def _slot_duration(slot):
    return int(slot.get("duration_min") or 57)


def _slot_overlap(a, b):
    if (a.get("day_of_week") or "") != (b.get("day_of_week") or ""):
        return False
    a_start = _slot_minutes(a.get("time") or "00:00")
    b_start = _slot_minutes(b.get("time") or "00:00")
    return a_start < b_start + _slot_duration(b) and b_start < a_start + _slot_duration(a)


def _location_region(location):
    if location in MUMBAI_LOCATIONS:
        return "mumbai"
    if location in BENGALURU_LOCATIONS:
        return "bengaluru"
    return location


def _shift_label(time_str):
    return "AM" if _slot_minutes(time_str) < 13 * 60 else "PM"


def _violates_location_shift_lock(slot, trainer_rows):
    loc = slot.get("location") or ""
    day = slot.get("day_of_week") or ""
    shift = _shift_label(slot.get("time") or "00:00")
    slot_min = _slot_minutes(slot.get("time") or "00:00")
    same_shift = [
        (_slot_minutes(r.get("time") or "00:00"), r.get("location") or "")
        for r in trainer_rows
        if r.get("day_of_week") == day and _shift_label(r.get("time") or "00:00") == shift
    ]
    if not same_shift:
        return ""
    if any(_location_region(existing_loc) != _location_region(loc) for _, existing_loc in same_shift):
        return f"{slot.get('trainer_1')} already has a class in another city on {day} {shift}"
    existing_main = {existing_loc for _, existing_loc in same_shift if existing_loc in MAIN_STUDIOS}
    if loc in MAIN_STUDIOS:
        if existing_main and any(existing_loc != loc for existing_loc in existing_main):
            return f"{slot.get('trainer_1')} is already assigned to another main studio on {day} {shift}"
        if any(existing_loc in DERIVED_STUDIOS for _, existing_loc in same_shift):
            return f"{slot.get('trainer_1')} cannot return to a main studio after a pop-up studio on {day} {shift}"
    elif loc in DERIVED_STUDIOS:
        if any(existing_loc in DERIVED_STUDIOS for _, existing_loc in same_shift):
            return f"{slot.get('trainer_1')} already has a pop-up studio assignment on {day} {shift}"
        if any(existing_min > slot_min for existing_min, _ in same_shift):
            return f"{slot.get('trainer_1')} can only take {loc} if it is their final stop on {day} {shift}"
    return ""


def _trainer_profile(name):
    if not TRAINER_PROFILES_PATH.exists():
        return None
    target = " ".join(str(name or "").split()).lower()
    profiles = json.loads(TRAINER_PROFILES_PATH.read_text())
    return next((p for p in profiles if " ".join(str(p.get("name") or "").split()).lower() == target), None)


def _profile_location_data(profile, loc):
    locations = profile.get("locations") or {}
    if loc in locations:
        return locations[loc]
    if loc == "Courtside":
        candidates = [locations.get(l) for l in ("Kwality House, Kemps Corner", "Supreme HQ, Bandra") if locations.get(l)]
    elif loc == "Copper & Cloves":
        candidates = [locations.get(l) for l in ("Copper & Cloves", "Kenkere House") if locations.get(l)]
    else:
        candidates = []
    return max(candidates, key=lambda item: item.get("session_count", 0)) if candidates else None


def _validate_manual_slot(data, iteration, slot, original_slot=None):
    trainer = slot.get("trainer_1") or ""
    profile = _trainer_profile(trainer)
    if not profile:
        raise ValueError(f"No active trainer profile found for {trainer}")
    if profile.get("active") is False:
        raise ValueError(f"{trainer} is inactive")
    loc = slot.get("location") or ""
    loc_profile = _profile_location_data(profile, loc)
    if not loc_profile:
        raise ValueError(f"{trainer} is not enabled for {loc}")
    day = slot.get("day_of_week") or ""
    days = loc_profile.get("available_days") or []
    if days and day not in days:
        raise ValueError(f"{trainer} is not available on {day}")
    tw = loc_profile.get("time_window") or {}
    start = tw.get("start", "06:00")
    end = tw.get("end", "22:00")
    slot_start = _slot_minutes(slot.get("time") or "00:00")
    if slot_start < _slot_minutes(start) or slot_start >= _slot_minutes(end):
        raise ValueError(f"{trainer} is outside their time window {start}-{end}")

    rows = []
    if iteration == "Main":
        for loc_rows in (data.get("locations") or {}).values():
            rows.extend(loc_rows or [])
    else:
        for loc_rows in ((data.get("iterations") or {}).get(iteration) or {}).values():
            rows.extend(loc_rows or [])
    rows = [r for r in rows if not (original_slot and _same_schedule_slot(r, original_slot))]
    trainer_rows = [r for r in rows if (r.get("trainer_1") or "").strip().lower() == trainer.strip().lower()]

    max_day = int(loc_profile.get("max_classes_per_day") or 4)
    same_day_loc = [r for r in trainer_rows if r.get("location") == loc and r.get("day_of_week") == day]
    if len(same_day_loc) >= max_day:
        raise ValueError(f"{trainer} already has {len(same_day_loc)}/{max_day} classes at {loc} on {day}")
    if any(_slot_overlap(slot, r) for r in trainer_rows):
        raise ValueError(f"{trainer} has an overlapping class on {day}")
    shift = "AM" if slot_start < 13 * 60 else "PM"
    opposite = "PM" if shift == "AM" else "AM"
    if any((r.get("day_of_week") == day) and (("AM" if _slot_minutes(r.get("time")) < 13 * 60 else "PM") == opposite) for r in trainer_rows):
        raise ValueError(f"{trainer} already has an {opposite} class on {day}")
    location_shift_error = _violates_location_shift_lock(slot, trainer_rows)
    if location_shift_error:
        raise ValueError(location_shift_error)
    weekly_minutes = sum(_slot_duration(r) for r in trainer_rows)
    if weekly_minutes + _slot_duration(slot) > 15 * 60:
        raise ValueError(f"{trainer} would exceed the 15h weekly cap")


def _replace_trainer_in_schedule(payload):
    slot = payload.get("slot") or {}
    new_trainer = str(payload.get("new_trainer") or "").strip()
    iteration = payload.get("iteration") or "Main"
    if not new_trainer:
        raise ValueError("Missing replacement trainer")
    required = ("location", "day_of_week", "time", "class_name", "trainer_1")
    missing = [k for k in required if not slot.get(k)]
    if missing:
        raise ValueError(f"Missing slot field(s): {', '.join(missing)}")

    path = WEB_DIR / "schedule_data.json"
    if not path.exists():
        raise FileNotFoundError("web/schedule_data.json was not found")
    data = json.loads(path.read_text())
    new_slot = dict(slot)
    new_slot["trainer_1"] = new_trainer
    if new_slot.get("location") in (MUMBAI_LOCATIONS | BENGALURU_LOCATIONS):
        _validate_manual_slot(data, iteration, new_slot, original_slot=slot)
    updated = 0

    def update_rows(rows):
        nonlocal updated
        if not isinstance(rows, list):
            return
        for row in rows:
            if _same_schedule_slot(row, slot):
                old_trainer = row.get("trainer_1") or ""
                row["replaced_from_trainer"] = old_trainer
                row["trainer_1"] = new_trainer
                row["recommendation"] = "MANUAL"
                row["scheduling_reason"] = (
                    f"Manual trainer replacement: {old_trainer or '—'} → {new_trainer}"
                )
                updated += 1

    if iteration == "Main":
        update_rows((data.get("locations") or {}).get(slot.get("location")))
    else:
        update_rows(((data.get("iterations") or {}).get(iteration) or {}).get(slot.get("location")))

    if updated == 0:
        raise ValueError("Could not find the selected class in the active schedule")

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)
    _regenerate_index_from_template(data)
    return updated


def _add_class_to_schedule(payload):
    slot = payload.get("slot") or {}
    iteration = payload.get("iteration") or "Main"
    required = ("location", "day_of_week", "time", "class_name", "trainer_1")
    missing = [k for k in required if not slot.get(k)]
    if missing:
        raise ValueError(f"Missing slot field(s): {', '.join(missing)}")

    path = WEB_DIR / "schedule_data.json"
    if not path.exists():
        raise FileNotFoundError("web/schedule_data.json was not found")
    data = json.loads(path.read_text())
    slot = dict(slot)
    slot.setdefault("recommendation", "MANUAL")
    slot.setdefault("manual_added", True)
    slot.setdefault("scheduling_reason", "Manual class added from calendar")
    _validate_manual_slot(data, iteration, slot)

    loc = slot["location"]
    if iteration == "Main":
        data.setdefault("locations", {}).setdefault(loc, []).append(slot)
    else:
        data.setdefault("iterations", {}).setdefault(iteration, {}).setdefault(loc, []).append(slot)

    supabase_saved = _write_schedule_data(data)
    return {"added": 1, "supabase_saved": supabase_saved}


def _save_schedule_to_supabase(data):
    if not supabase_configured():
        return {"saved": False, "error": "Supabase is not configured"}
    try:
        supabase_upsert("saved_schedule", data)
        return {"saved": True, "error": ""}
    except Exception as exc:
        return {"saved": False, "error": str(exc)}


def _write_schedule_data(data):
    path = WEB_DIR / "schedule_data.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)
    _regenerate_index_from_template(data)
    return _save_schedule_to_supabase(data)


def _finalise_schedule_to_supabase():
    return finalise_schedule_document(
        supabase_request,
        schedule_path=WEB_DIR / "schedule_data.json",
        outputs_dir=OUTPUTS_DIR,
    )


def _rows_for_iteration(data, iteration, loc):
    if iteration == "Main":
        return data.setdefault("locations", {}).setdefault(loc, [])
    return data.setdefault("iterations", {}).setdefault(iteration, {}).setdefault(loc, [])


def _clear_schedule(payload):
    iteration = (payload or {}).get("iteration") or "Main"
    path = WEB_DIR / "schedule_data.json"
    data = json.loads(path.read_text())
    if iteration == "Main":
        for loc in list((data.get("locations") or {}).keys()):
            data.setdefault("locations", {})[loc] = []
    else:
        for loc in list(((data.get("iterations") or {}).get(iteration) or {}).keys()):
            data.setdefault("iterations", {}).setdefault(iteration, {})[loc] = []
    supabase_saved = _write_schedule_data(data)
    return {"cleared": True, "supabase_saved": supabase_saved}


def _remove_class_from_schedule(payload):
    slot = payload.get("slot") or {}
    iteration = payload.get("iteration") or "Main"
    path = WEB_DIR / "schedule_data.json"
    data = json.loads(path.read_text())
    rows = _rows_for_iteration(data, iteration, slot.get("location"))
    idx = next((i for i, row in enumerate(rows) if _same_schedule_slot(row, slot)), -1)
    if idx < 0:
        raise ValueError("Could not find the selected class in the active schedule")
    rows.pop(idx)
    supabase_saved = _write_schedule_data(data)
    return {"removed": 1, "supabase_saved": supabase_saved}


def _move_class_in_schedule(payload):
    slot = payload.get("slot") or {}
    target = payload.get("target") or {}
    iteration = payload.get("iteration") or "Main"
    for key in ("location", "day_of_week", "time"):
        if not target.get(key):
            raise ValueError(f"Missing target field: {key}")
    path = WEB_DIR / "schedule_data.json"
    data = json.loads(path.read_text())
    rows = _rows_for_iteration(data, iteration, slot.get("location"))
    idx = next((i for i, row in enumerate(rows) if _same_schedule_slot(row, slot)), -1)
    if idx < 0:
        raise ValueError("Could not find the selected class in the active schedule")
    moved = dict(rows.pop(idx))
    moved.update({
        "location": target["location"],
        "date": target.get("date", moved.get("date", "")),
        "day_of_week": target["day_of_week"],
        "time": target["time"],
        "recommendation": "MANUAL",
        "manual_moved": True,
        "scheduling_reason": (
            f"Manual drag/drop move: {slot.get('day_of_week')} {slot.get('time')} → "
            f"{target.get('day_of_week')} {target.get('time')}"
        ),
    })
    _validate_manual_slot(data, iteration, moved, original_slot=slot)
    _rows_for_iteration(data, iteration, target["location"]).append(moved)
    supabase_saved = _write_schedule_data(data)
    return {"moved": 1, "slot": moved, "supabase_saved": supabase_saved}


def _regenerate_index_from_template(schedule_data=None):
    from agents.reporter import OPTIMISATION_OPPORTUNITIES, _rules_panel_html

    template_path = WEB_DIR / "template.html"
    schedule_path = WEB_DIR / "schedule_data.json"
    if not template_path.exists() or not schedule_path.exists():
        return
    schedule_json = schedule_path.read_text(encoding="utf-8")
    data = schedule_data or json.loads(schedule_json)
    all_rows = []
    for rows in (data.get("locations") or {}).values():
        all_rows.extend(rows or [])
    first_date = min((r.get("date") for r in all_rows if r.get("date")), default=date.today().isoformat())
    ws = date.fromisoformat(first_date)
    week_start = ws - timedelta(days=ws.weekday())
    week_end = week_start + timedelta(days=6)
    week_label = f"{week_start.strftime('%d %b')} – {week_end.strftime('%d %b %Y')}"
    scorecard_path = OUTPUTS_DIR / "scorecard.json"
    scorecard = json.loads(scorecard_path.read_text(encoding="utf-8")) if scorecard_path.exists() else None
    scorecard_json = json.dumps(scorecard, indent=2) if scorecard else "null"
    html = (
        template_path.read_text(encoding="utf-8")
        .replace("/*INJECT_SCHEDULE_DATA*/", schedule_json)
        .replace("/*INJECT_SCORECARD*/", scorecard_json)
        .replace("/*INJECT_WEEK_LABEL*/", f'"{week_label}"')
        .replace("/*INJECT_OPPORTUNITIES*/", json.dumps(OPTIMISATION_OPPORTUNITIES))
    )
    html = html.replace("</body>", _rules_panel_html() + "\n</body>", 1)
    (WEB_DIR / "index.html").write_text(html, encoding="utf-8")


def _pipeline_process_alive(pid) -> bool:
    if not pid:
        return False
    try:
        pid_int = int(pid)
        os.kill(pid_int, 0)
    except (OSError, TypeError, ValueError):
        return False

    if os.name == "posix":
        try:
            result = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid_int)],
                capture_output=True,
                text=True,
                timeout=1,
            )
            stat = result.stdout.strip()
            if stat and stat[0] in {"T", "Z"}:
                return False
        except Exception:
            pass
    return True


def _refresh_pipeline_state(state=None) -> None:
    state = state or _pipeline_state
    if not state.get("running"):
        return
    if _pipeline_process_alive(state.get("pid")):
        return
    state["running"] = False
    state["status"] = "failed"
    state["pid"] = None
    state["message"] = "Previous pipeline process stopped before completion. Generate can be run again."

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def find_available_port(start_port: int = 8080, host: str = "", max_tries: int = 100) -> int:
    """Return the first bindable port at or above start_port."""
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No available port found from {start_port} to {start_port + max_tries - 1}")


def resolve_port(port_arg: str) -> int:
    if str(port_arg).lower() == "auto":
        return find_available_port(8080, host="")
    port = int(port_arg)
    if port == 0:
        return find_available_port(8080, host="")
    return port


def create_server(port_arg: str) -> tuple[HTTPServer, int]:
    """Create the HTTP server, retrying if an auto-selected port is raced."""
    if str(port_arg).lower() not in {"auto", "0"}:
        port = int(port_arg)
        return HTTPServer(("", port), RulesHandler), port

    last_error = None
    start_port = 8080
    for _ in range(100):
        port = find_available_port(start_port, host="")
        try:
            return HTTPServer(("", port), RulesHandler), port
        except OSError as exc:
            last_error = exc
            start_port = port + 1
    raise RuntimeError("Could not bind an available port") from last_error


def build_pipeline_command(csv_path: str, week: str, variation_seed: int, output_suffix: str) -> list[str]:
    return [
        sys.executable, str(PROJECT_ROOT / "orchestrator.py"),
        "--csv", csv_path,
        "--week", week,
        "--debug",
        "--variation-seed", str(variation_seed),
        "--output-suffix", output_suffix,
    ]


def is_output_artifact_path(path: str) -> bool:
    """Return true only for generated files that live under outputs/."""
    filename = path.lstrip("/")
    if filename in {"ai_insights.json", "scorecard.json"}:
        return True
    if not filename.startswith("schedule_"):
        return False
    if filename.startswith("schedule_data"):
        return False
    return filename.endswith((".csv", ".xlsx", ".json"))


def supabase_settings() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if url.endswith("/rest/v1"):
        url = url[: -len("/rest/v1")]
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    return url, key


def supabase_configured() -> bool:
    url, key = supabase_settings()
    return bool(url and key)


def supabase_request(method: str, path: str, body=None, prefer: str | None = None):
    url, key = supabase_settings()
    if not url or not key:
        raise RuntimeError("Supabase backend config is missing SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
    payload = json.dumps(body).encode() if body is not None else None
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    elif method != "GET":
        headers["Prefer"] = "return=representation"
    req = urlrequest.Request(f"{url}/rest/v1{path}", data=payload, method=method, headers=headers)
    try:
        with urlrequest.urlopen(req, timeout=20) as resp:
            data = resp.read().decode()
    except urlerror.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(detail or str(exc)) from exc
    return json.loads(data) if data else {}


def supabase_upsert(config_key: str, data):
    return supabase_request(
        "POST",
        "/studio_rules?on_conflict=config_key",
        [{"config_key": config_key, "data": data}],
        "resolution=merge-duplicates,return=representation",
    )


def load_json_file(path: Path, fallback):
    if not path.exists():
        return fallback
    with open(path) as f:
        return json.load(f)


def push_supabase_config():
    schedule_config = load_json_file(
        SCHEDULE_CONFIG_PATH,
        {"targets": {}, "manual_protected": [], "manual_excluded": [], "custom_rules": []},
    )
    trainer_profiles = load_json_file(TRAINER_PROFILES_PATH, [])
    rules_catalog = build_rules_catalog(load_rules_config())
    supabase_upsert("schedule_config", schedule_config)
    supabase_upsert("trainer_profiles", trainer_profiles)
    supabase_upsert("rules_catalog", rules_catalog)
    studio_groups = {
        "rules_kwality": ["location_kwality"],
        "rules_supreme": ["location_supreme"],
        "rules_kenkere": ["location_kenkere"],
    }
    for key, group_ids in studio_groups.items():
        groups = [g for g in rules_catalog.get("groups", []) if g.get("id") in group_ids]
        supabase_upsert(key, {"groups": groups})
    return {"schedule_config": schedule_config, "trainer_profiles": trainer_profiles, "rules_catalog": rules_catalog}


def pull_supabase_config():
    rows = supabase_request("GET", "/studio_rules?select=config_key,data")
    pulled = {row.get("config_key"): row.get("data") for row in rows if isinstance(row, dict)}
    if pulled.get("schedule_config"):
        SCHEDULE_CONFIG_PATH.parent.mkdir(exist_ok=True)
        SCHEDULE_CONFIG_PATH.write_text(json.dumps(pulled["schedule_config"], indent=2))
    if pulled.get("trainer_profiles"):
        TRAINER_PROFILES_PATH.write_text(json.dumps(pulled["trainer_profiles"], indent=2))
    return pulled


class RulesHandler(BaseHTTPRequestHandler):
    # Set via class variable from CLI arg
    pipeline_week: str = "2026-05-04"
    pipeline_csv: str = "Class Performance by Trainer.csv"

    def log_message(self, format, *args):
        print(f"  [{self.address_string()}] {format % args}")

    def _send(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self._send(code, "application/json", body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # API: get current rules config
        if path == "/api/rules-config":
            self._send_json(200, build_rules_catalog(load_rules_config()))
            return

        if path == "/api/supabase/status":
            url, key = supabase_settings()
            self._send_json(200, {
                "configured": bool(url and key),
                "has_url": bool(url),
                "has_key": bool(key),
                "url_host": urlparse(url).netloc if url else "",
            })
            return

        # API: pipeline status
        if path == "/api/pipeline-status":
            _refresh_pipeline_state()
            msg = _pipeline_state["message"]
            running = _pipeline_state["running"]
            status = _pipeline_state.get("status", "running" if running else "idle")
            self._send_json(200, {
                "running": running,
                "status": status,
                "pid": _pipeline_state["pid"],
                "started": _pipeline_state["started"],
                "message": msg,
            })
            return

        if path == "/api/latest-schedule-file":
            self._send_json(200, {"file": "schedule_data.json"})
            return

        # API: trainer profiles
        if path == "/api/trainer-profiles":
            profiles_path = PROJECT_ROOT / "rules" / "trainer_profiles.json"
            if profiles_path.exists():
                self._send(200, "application/json", profiles_path.read_bytes())
            else:
                self._send_json(200, [])
            return

        # API: historic slot scores (03_scores.json class_slot_ranking)
        if path == "/api/historic-slots":
            scores_path = PROJECT_ROOT / "state" / "03_scores.json"
            if scores_path.exists():
                self._send(200, "application/json", scores_path.read_bytes())
            else:
                self._send_json(200, {"class_slot_ranking": [], "trainer_metrics": [], "class_metrics": []})
            return

        # API: schedule config (targets, manual protections)
        if path == "/api/schedule-config":
            cfg_path = PROJECT_ROOT / "config" / "schedule_config.json"
            if cfg_path.exists():
                self._send(200, "application/json", cfg_path.read_bytes())
            else:
                default = {"targets": {}, "manual_protected": [], "manual_excluded": [], "custom_rules": []}
                self._send_json(200, default)
            return

        # Serve root → web/index.html
        if path == "/":
            self._serve_file(WEB_DIR / "index.html")
            return

        # Serve outputs/ files (schedule CSVs, XLSXs, JSON)
        if is_output_artifact_path(path):
            filename = path.lstrip("/")
            self._serve_file(OUTPUTS_DIR / filename)
            return

        # Serve web/ static assets
        rel = path.lstrip("/")
        candidate = WEB_DIR / rel
        if candidate.exists() and candidate.is_file():
            self._serve_file(candidate)
            return

        self._send_json(404, {"error": f"Not found: {path}"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        length = int(self.headers.get("Content-Length", 0))
        body_raw = self.rfile.read(length) if length else b"{}"

        if path == "/api/save-rules":
            try:
                payload = json.loads(body_raw)
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": f"Invalid JSON: {e}"})
                return
            current = update_rules_config(payload)
            catalog = build_rules_catalog(current)
            print("  [API] Rules config saved")
            self._send_json(200, {"ok": True, "config": current, "catalog": catalog})
            return

        if path == "/api/supabase/test":
            try:
                supabase_request("GET", "/studio_rules?limit=1")
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/supabase/push":
            try:
                data = push_supabase_config()
                self._send_json(200, {"ok": True, **data})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/supabase/pull":
            try:
                data = pull_supabase_config()
                self._send_json(200, {"ok": True, "data": data})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/run-pipeline":
            global _run_counter
            _refresh_pipeline_state()
            if _pipeline_state["running"]:
                self._send_json(200, {
                    "ok": True,
                    "already_running": True,
                    "message": "Pipeline is already running. Please wait.",
                })
                return
            _run_counter += 1
            csv_path = self.pipeline_csv
            week = self.pipeline_week
            variation_seed = int(_time.time()) % 100000 + _run_counter
            output_suffix = f"run{_run_counter}_{uuid.uuid4().hex[:6]}"
            cmd = build_pipeline_command(csv_path, week, variation_seed, output_suffix)
            print(f"  [API] Spawning pipeline: {' '.join(cmd)}")
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(PROJECT_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                _pipeline_state["running"] = True
                _pipeline_state["status"] = "running"
                _pipeline_state["pid"] = proc.pid
                _pipeline_state["started"] = _time.time()
                _pipeline_state["message"] = "Running — Agent 1: Ingesting data..."

                def _monitor(p, state):
                    stage_markers = [
                        ("Agent 1", "Running — Agent 1: Ingesting data..."),
                        ("Agent 2", "Running — Agent 2: Analysing history..."),
                        ("Agent 3", "Running — Agent 3: Scoring slots..."),
                        ("Agent 4", "Running — Agent 4: Applying rules..."),
                        ("Agent 6", "Running — Agent 6: Building report..."),
                    ]
                    tail = []
                    for line in p.stdout or []:
                        clean = line.rstrip()
                        if not clean:
                            continue
                        print(f"  [PIPELINE] {clean}")
                        tail.append(clean)
                        tail = tail[-20:]
                        if "[Agent 5]" in clean:
                            inner = clean.split("[Agent 5]", 1)[-1].strip()
                            state["message"] = f"Optimising schedule — {inner}"
                            continue
                        for marker, message in stage_markers:
                            if marker in clean:
                                state["message"] = message
                                break
                    p.wait()
                    if p.returncode == 0:
                        state["status"] = "done"
                        state["message"] = "Complete — reload to see new schedule."
                    else:
                        state["status"] = "failed"
                        detail = next((item for item in reversed(tail) if item.strip()), "")
                        state["message"] = f"Failed (exit {p.returncode}). {detail or 'Check server logs.'}"
                    state["running"] = False
                    state["pid"] = None

                t = threading.Thread(target=_monitor, args=(proc, _pipeline_state), daemon=True)
                t.start()

                self._send_json(200, {
                    "ok": True,
                    "pid": proc.pid,
                    "message": "Pipeline started. Results ready in ~2 minutes.",
                })
            except Exception as e:
                _pipeline_state["running"] = False
                _pipeline_state["status"] = "failed"
                self._send_json(500, {"error": str(e)})
            return

        # API: save trainer profiles
        if path == "/api/save-trainer-profiles":
            try:
                payload = json.loads(body_raw)
                profiles_path = PROJECT_ROOT / "rules" / "trainer_profiles.json"
                with open(profiles_path, "w") as f:
                    json.dump(payload, f, indent=2)
                print("  [API] Trainer profiles saved")
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        # API: save schedule config
        if path == "/api/save-schedule-config":
            try:
                payload = json.loads(body_raw)
                cfg_path = PROJECT_ROOT / "config" / "schedule_config.json"
                with open(cfg_path, "w") as f:
                    json.dump(payload, f, indent=2)
                print("  [API] Schedule config saved")
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        # API: one-click trainer replacement for the active generated schedule
        if path == "/api/replace-trainer":
            try:
                payload = json.loads(body_raw)
                updated = _replace_trainer_in_schedule(payload)
                print(f"  [API] Trainer replaced in {updated} schedule row(s)")
                self._send_json(200, {"ok": True, "updated": updated})
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": f"Invalid JSON: {e}"})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        # API: manually add a class to the active generated schedule
        if path == "/api/add-class":
            try:
                payload = json.loads(body_raw)
                result = _add_class_to_schedule(payload)
                print(f"  [API] Manual class added in {result.get('added', 0)} schedule row(s)")
                self._send_json(200, {"ok": True, **result})
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": f"Invalid JSON: {e}"})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        # API: remove a class from the active generated schedule
        if path == "/api/remove-class":
            try:
                payload = json.loads(body_raw)
                result = _remove_class_from_schedule(payload)
                print(f"  [API] Manual class removed in {result.get('removed', 0)} schedule row(s)")
                self._send_json(200, {"ok": True, **result})
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": f"Invalid JSON: {e}"})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        # API: drag/drop move a class in the active generated schedule
        if path == "/api/move-class":
            try:
                payload = json.loads(body_raw)
                result = _move_class_in_schedule(payload)
                print(f"  [API] Manual class moved in {result.get('moved', 0)} schedule row(s)")
                self._send_json(200, {"ok": True, **result})
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": f"Invalid JSON: {e}"})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        if path == "/api/clear-schedule":
            try:
                payload = json.loads(body_raw or "{}")
                result = _clear_schedule(payload)
                print("  [API] Schedule cleared")
                self._send_json(200, {"ok": True, **result})
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": f"Invalid JSON: {e}"})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        # API: explicitly save current generated schedule to Supabase
        if path == "/api/save-schedule-supabase":
            try:
                data = json.loads((WEB_DIR / "schedule_data.json").read_text())
                self._send_json(200, {"ok": True, "supabase_saved": _save_schedule_to_supabase(data)})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        if path == "/api/finalise-schedule":
            try:
                result = _finalise_schedule_to_supabase()
                self._send_json(200, {"ok": True, "finalised": result})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        self._send_json(404, {"error": f"Unknown endpoint: {path}"})

    def _serve_file(self, file_path: Path):
        if not file_path.exists():
            self._send_json(404, {"error": f"File not found: {file_path.name}"})
            return
        ext = file_path.suffix.lower()
        content_type = MIME_TYPES.get(ext, "application/octet-stream")
        body = file_path.read_bytes()
        self._send(200, content_type, body)


def main():
    parser = argparse.ArgumentParser(description="Studio Scheduler local web server")
    parser.add_argument("--port", default="auto", help="Port to listen on, or 'auto' to choose from 8080 upward")
    parser.add_argument("--week", default="2026-05-04", help="Default week for pipeline re-run")
    parser.add_argument("--csv", default="Sessions Performance Data.csv",
                        help="CSV path for pipeline re-run")
    args = parser.parse_args()

    RulesHandler.pipeline_week = args.week
    RulesHandler.pipeline_csv = args.csv

    HTTPServer.allow_reuse_address = True
    server, port = create_server(args.port)
    print(f"\n  Studio Scheduler — serving at http://localhost:{port}")
    print(f"  Pipeline week: {args.week} | CSV: {args.csv}")
    print(f"  Rule toggles: http://localhost:{port}  (Rules panel)")
    print(f"  Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")


if __name__ == "__main__":
    main()
