"""
app.py — Flask WSGI entrypoint for Studio Scheduler.
Replaces serve.py's raw HTTP server for autoscale deployment.

Week and CSV are read from env vars PIPELINE_WEEK / PIPELINE_CSV,
falling back to sensible defaults (next Monday, bundled CSV).
"""
import json
import os
import subprocess
import sys
import threading
import time as _time
import uuid
from datetime import date, timedelta
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from flask import Flask, Response, request

from finalise_schedule import finalise_schedule_document
from rule_config import build_rules_catalog, load_rules_config, update_rules_config

PROJECT_ROOT = Path(__file__).parent
WEB_DIR = PROJECT_ROOT / "web"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
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

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}

# ── Pipeline defaults ─────────────────────────────────────────

def _next_monday() -> str:
    today = date.today()
    days_ahead = (7 - today.weekday()) % 7 or 7
    return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

PIPELINE_CSV = os.environ.get("PIPELINE_CSV", "Sessions Performance Data.csv")
PIPELINE_WEEK = os.environ.get("PIPELINE_WEEK") or _next_monday()

# In-memory pipeline state (per worker process)
_pipeline_state = {
    "running": False,
    "status": "idle",
    "pid": None,
    "started": None,
    "message": "Idle",
}
_run_counter = 0
_latest_schedule_file = "schedule_data.json"

# ── Flask app ─────────────────────────────────────────────────

app = Flask(__name__, static_folder=None)


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


def _json(data, status=200):
    return Response(json.dumps(data), status=status, mimetype="application/json")


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
    if not all(supabase_settings()):
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


def _file(path: Path):
    if not path.exists():
        return _json({"error": f"Not found: {path.name}"}, 404)
    mime = MIME.get(path.suffix.lower(), "application/octet-stream")
    return Response(path.read_bytes(), mimetype=mime)


def is_output_artifact_name(name: str) -> bool:
    if name in {"ai_insights.json", "scorecard.json"}:
        return True
    if not name.startswith("schedule_"):
        return False
    if name.startswith("schedule_data"):
        return False
    return name.endswith((".csv", ".xlsx", ".json"))


def supabase_settings() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if url.endswith("/rest/v1"):
        url = url[: -len("/rest/v1")]
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    return url, key


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


# ── GET routes ────────────────────────────────────────────────

@app.route("/")
def index():
    return _file(WEB_DIR / "index.html")


@app.route("/api/rules-config")
def rules_config():
    return _json(build_rules_catalog(load_rules_config()))


@app.route("/api/supabase/status")
def supabase_status():
    url, key = supabase_settings()
    from urllib.parse import urlparse
    return _json({
        "configured": bool(url and key),
        "has_url": bool(url),
        "has_key": bool(key),
        "url_host": urlparse(url).netloc if url else "",
    })


@app.route("/api/pipeline-status")
def pipeline_status():
    _refresh_pipeline_state()
    msg = _pipeline_state["message"]
    running = _pipeline_state["running"]
    status = _pipeline_state.get("status", "running" if running else "idle")
    return _json({
        "running": running,
        "status": status,
        "pid": _pipeline_state["pid"],
        "started": _pipeline_state["started"],
        "message": msg,
    })


@app.route("/api/trainer-profiles")
def trainer_profiles():
    p = PROJECT_ROOT / "rules" / "trainer_profiles.json"
    return _file(p) if p.exists() else _json([])


@app.route("/api/historic-slots")
def historic_slots():
    p = PROJECT_ROOT / "state" / "03_scores.json"
    return _file(p) if p.exists() else _json(
        {"class_slot_ranking": [], "trainer_metrics": [], "class_metrics": []})


@app.route("/api/schedule-config")
def schedule_config():
    p = PROJECT_ROOT / "config" / "schedule_config.json"
    if p.exists():
        return _file(p)
    return _json({"targets": {}, "manual_protected": [], "manual_excluded": [], "custom_rules": []})


@app.route("/schedule_<path:fname>")
@app.route("/ai_<path:fname>")
@app.route("/scorecard<path:fname>")
def output_file(fname=""):
    name = request.path.lstrip("/")
    if is_output_artifact_name(name):
        return _file(OUTPUTS_DIR / name)
    candidate = WEB_DIR / name
    if candidate.exists() and candidate.is_file():
        return _file(candidate)
    return _json({"error": f"Not found: {name}"}, 404)


@app.route("/api/latest-schedule-file")
def latest_schedule_file():
    return _json({"file": _latest_schedule_file})


@app.route("/<path:name>")
def static_file(name):
    candidate = WEB_DIR / name
    if candidate.exists() and candidate.is_file():
        return _file(candidate)
    return _json({"error": f"Not found: {name}"}, 404)


# ── POST routes ───────────────────────────────────────────────

@app.route("/api/save-rules", methods=["POST"])
def save_rules():
    try:
        payload = request.get_json(force=True)
        current = update_rules_config(payload)
        catalog = build_rules_catalog(current)
        return _json({"ok": True, "config": current, "catalog": catalog})
    except Exception as e:
        return _json({"error": str(e)}, 500)


@app.route("/api/supabase/test", methods=["POST"])
def supabase_test():
    try:
        supabase_request("GET", "/studio_rules?limit=1")
        return _json({"ok": True})
    except Exception as e:
        return _json({"ok": False, "error": str(e)}, 500)


@app.route("/api/supabase/push", methods=["POST"])
def supabase_push():
    try:
        data = push_supabase_config()
        return _json({"ok": True, **data})
    except Exception as e:
        return _json({"ok": False, "error": str(e)}, 500)


@app.route("/api/supabase/pull", methods=["POST"])
def supabase_pull():
    try:
        data = pull_supabase_config()
        return _json({"ok": True, "data": data})
    except Exception as e:
        return _json({"ok": False, "error": str(e)}, 500)


@app.route("/api/run-pipeline", methods=["POST"])
def run_pipeline():
    global _run_counter
    _refresh_pipeline_state()
    if _pipeline_state["running"]:
        return _json({
            "ok": True,
            "already_running": True,
            "message": "Pipeline is already running. Please wait.",
        })

    _run_counter += 1
    variation_seed = int(_time.time()) % 100000 + _run_counter
    output_suffix = f"run{_run_counter}_{uuid.uuid4().hex[:6]}"
    csv_path = PIPELINE_CSV
    week = PIPELINE_WEEK
    cmd = [
        sys.executable, str(PROJECT_ROOT / "orchestrator.py"),
        "--csv", csv_path,
        "--week", week,
        "--debug",
        "--variation-seed", str(variation_seed),
        "--output-suffix", output_suffix,
    ]
    print(f"  [API] Spawning pipeline: {' '.join(cmd)}")
    try:
        global _latest_schedule_file
        _latest_schedule_file = "schedule_data.json"
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
            stage_prefixes = [
                ("Agent 1", "Ingesting session data"),
                ("Agent 2", "Analysing performance history"),
                ("Agent 3", "Scoring class combinations"),
                ("Agent 4", "Applying scheduling rules"),
                ("Agent 6", "Building final report"),
            ]
            tail = []
            for line in p.stdout or []:
                clean = line.rstrip()
                if not clean:
                    continue
                print(f"  [PIPELINE] {clean}")
                tail.append(clean)
                tail = tail[-30:]
                # Agent 5 — show per-location/day progress verbatim
                if "[Agent 5]" in clean:
                    inner = clean.split("[Agent 5]", 1)[-1].strip()
                    state["message"] = f"Optimising schedule — {inner}"
                    continue
                for marker, label in stage_prefixes:
                    if marker in clean:
                        state["message"] = f"Running — {label}..."
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

        return _json({
            "ok": True,
            "pid": proc.pid,
            "message": "Pipeline started. Results ready in ~2 minutes.",
        })
    except Exception as e:
        _pipeline_state["running"] = False
        _pipeline_state["status"] = "failed"
        return _json({"error": str(e)}, 500)


@app.route("/api/save-trainer-profiles", methods=["POST"])
def save_trainer_profiles():
    try:
        payload = request.get_json(force=True)
        p = PROJECT_ROOT / "rules" / "trainer_profiles.json"
        p.write_text(json.dumps(payload, indent=2))
        return _json({"ok": True})
    except Exception as e:
        return _json({"error": str(e)}, 500)


@app.route("/api/save-schedule-config", methods=["POST"])
def save_schedule_config():
    try:
        payload = request.get_json(force=True)
        p = PROJECT_ROOT / "config" / "schedule_config.json"
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps(payload, indent=2))
        return _json({"ok": True})
    except Exception as e:
        return _json({"error": str(e)}, 500)


@app.route("/api/replace-trainer", methods=["POST"])
def replace_trainer():
    try:
        payload = request.get_json(force=True)
        updated = _replace_trainer_in_schedule(payload)
        return _json({"ok": True, "updated": updated})
    except Exception as e:
        return _json({"error": str(e)}, 400)


@app.route("/api/add-class", methods=["POST"])
def add_class():
    try:
        payload = request.get_json(force=True)
        result = _add_class_to_schedule(payload)
        return _json({"ok": True, **result})
    except Exception as e:
        return _json({"error": str(e)}, 400)


@app.route("/api/remove-class", methods=["POST"])
def remove_class():
    try:
        payload = request.get_json(force=True)
        result = _remove_class_from_schedule(payload)
        return _json({"ok": True, **result})
    except Exception as e:
        return _json({"error": str(e)}, 400)


@app.route("/api/move-class", methods=["POST"])
def move_class():
    try:
        payload = request.get_json(force=True)
        result = _move_class_in_schedule(payload)
        return _json({"ok": True, **result})
    except Exception as e:
        return _json({"error": str(e)}, 400)


@app.route("/api/clear-schedule", methods=["POST"])
def clear_schedule():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        result = _clear_schedule(payload)
        return _json({"ok": True, **result})
    except Exception as e:
        return _json({"error": str(e)}, 400)


@app.route("/api/save-schedule-supabase", methods=["POST"])
def save_schedule_supabase():
    try:
        data = json.loads((WEB_DIR / "schedule_data.json").read_text())
        return _json({"ok": True, "supabase_saved": _save_schedule_to_supabase(data)})
    except Exception as e:
        return _json({"error": str(e)}, 400)


@app.route("/api/finalise-schedule", methods=["POST"])
def finalise_schedule():
    try:
        return _json({"ok": True, "finalised": _finalise_schedule_to_supabase()})
    except Exception as e:
        return _json({"error": str(e)}, 400)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
