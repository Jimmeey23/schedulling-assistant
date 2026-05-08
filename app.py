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

from chat_assistant import build_chat_context
from finalise_schedule import finalise_schedule_document
from rule_config import build_rules_catalog, load_rules_config, update_rules_config

PROJECT_ROOT = Path(__file__).parent
WEB_DIR = PROJECT_ROOT / "web"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
SCHEDULE_CONFIG_PATH = PROJECT_ROOT / "config" / "schedule_config.json"
TRAINER_PROFILES_PATH = PROJECT_ROOT / "rules" / "trainer_profiles.json"
DEFAULT_SCHEDULE_CONFIG_PATH = SCHEDULE_CONFIG_PATH
DEFAULT_TRAINER_PROFILES_PATH = TRAINER_PROFILES_PATH
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


def _is_local_request() -> bool:
    remote = request.remote_addr or ""
    return remote in {"127.0.0.1", "::1", "localhost"}


def _require_admin_for_unsafe_request():
    if request.method != "POST":
        return None
    if _is_local_request():
        return None
    token = os.environ.get("SCHEDULER_ADMIN_TOKEN", "")
    provided = request.headers.get("X-Scheduler-Admin-Token", "")
    if token and provided == token:
        return None
    return _json({"error": "Admin token required"}, 401)


app.before_request(_require_admin_for_unsafe_request)


def _safe_child_path(base: Path, name: str) -> Path | None:
    base_resolved = base.resolve()
    candidate = (base_resolved / name).resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        return None
    return candidate


def _schedule_config_path() -> Path:
    if SCHEDULE_CONFIG_PATH != DEFAULT_SCHEDULE_CONFIG_PATH:
        return SCHEDULE_CONFIG_PATH
    return PROJECT_ROOT / "config" / "schedule_config.json"


def _normalize_pipeline_week(value: str | None) -> str:
    raw = str(value or "").strip() or PIPELINE_WEEK
    try:
        selected = date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise ValueError("Please choose a valid schedule date.") from exc
    monday = selected - timedelta(days=selected.weekday())
    return monday.isoformat()


def _saved_ai_api_key() -> str:
    try:
        data = json.loads(_schedule_config_path().read_text())
    except Exception:
        return ""
    settings = data.get("settings_options") or {}
    return str(settings.get("ai_api_key") or "").strip()

def _saved_ai_runtime_settings() -> dict:
    try:
        data = json.loads(_schedule_config_path().read_text())
    except Exception:
        return {}
    settings = data.get("settings_options") or {}
    return {
        "provider": str(settings.get("ai_provider") or "openrouter").strip().lower(),
        "model": str(settings.get("ai_model") or "").strip(),
        "base_url": str(settings.get("ai_base_url") or "").strip(),
    }


def _build_chat_context() -> str:
    return build_chat_context(
        WEB_DIR / "schedule_data.json",
        OUTPUTS_DIR / "scorecard.json",
        _trainer_profiles_path(),
    )


def _build_chat_reply(payload: dict) -> str:
    user_msg = str((payload or {}).get("message") or "").strip()
    if not user_msg:
        raise ValueError("Empty message")

    try:
        from ai_provider import OPENAI_AVAILABLE, create_ai_client
    except Exception as exc:
        return f"AI client not available: {exc}"

    if not OPENAI_AVAILABLE:
        return "AI client not available. Install the openai package and set an AI API key to enable chat."

    client, settings = create_ai_client()
    if not client:
        api_key = _saved_ai_api_key()
        runtime = _saved_ai_runtime_settings()
        if api_key:
            try:
                from openai import OpenAI
                provider = runtime.get("provider") or "openrouter"
                base_url = runtime.get("base_url") or (
                    "https://api.openai.com/v1" if provider == "openai" else "https://openrouter.ai/api/v1"
                )
                model = runtime.get("model") or (
                    os.environ.get("OPENAI_MODEL") if provider == "openai" else os.environ.get("OPENROUTER_MODEL")
                ) or "openai/gpt-oss-120b:free"
                client = OpenAI(
                    api_key=api_key,
                    base_url=base_url,
                    default_headers={
                        "HTTP-Referer": "https://studio-scheduler.local",
                        "X-Title": "Studio Scheduler",
                    },
                )
                settings = {"model": model}
            except Exception:
                client = None

    if not client:
        return "AI not configured. Add an AI API key in Control Center or set OPENROUTER_API_KEY/OPENAI_API_KEY."

    messages = [{"role": "system", "content": build_chat_context(
        WEB_DIR / "schedule_data.json",
        OUTPUTS_DIR / "scorecard.json",
        _trainer_profiles_path(),
        user_msg,
    )}]
    for item in ((payload or {}).get("history") or [])[-6:]:
        role = item.get("role", "user")
        content = item.get("content", "")
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_msg})

    try:
        response = client.chat.completions.create(
            model=(settings or {}).get("model") or "openai/gpt-oss-120b:free",
            temperature=0.4,
            max_tokens=800,
            messages=messages,
        )
        return response.choices[0].message.content.strip() if response.choices else "No response from AI."
    except Exception as exc:
        return f"AI error: {exc}"

def _inject_ai_key_env(child_env: dict, api_key: str) -> None:
    key = str(api_key or "").strip()
    if not key:
        return
    # Set both vars so either provider path can work.
    child_env["OPENROUTER_API_KEY"] = key
    child_env["OPENAI_API_KEY"] = key

def _inject_ai_runtime_env(child_env: dict, runtime: dict) -> None:
    provider = str(runtime.get("provider") or "openrouter").strip().lower()
    model = str(runtime.get("model") or "").strip()
    base_url = str(runtime.get("base_url") or "").strip()
    if provider == "openai":
        if model:
            child_env["OPENAI_MODEL"] = model
        if base_url:
            child_env["OPENAI_BASE_URL"] = base_url
    else:
        if model:
            child_env["OPENROUTER_MODEL"] = model
        if base_url:
            child_env["OPENROUTER_BASE_URL"] = base_url


def _request_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _resolve_pipeline_request_options(payload=None) -> dict:
    payload = payload or {}
    week = _normalize_pipeline_week(
        payload.get("week_start") or payload.get("week") or payload.get("date")
    )
    use_ai = _request_bool(payload.get("use_ai"))
    child_env = os.environ.copy()
    child_env["PIPELINE_WEEK"] = week

    if use_ai:
        api_key = str(payload.get("api_key") or "").strip() or _saved_ai_api_key()
        runtime = _saved_ai_runtime_settings()
        payload_provider = str(payload.get("ai_provider") or "").strip().lower()
        payload_model = str(payload.get("ai_model") or "").strip()
        payload_base_url = str(payload.get("ai_base_url") or "").strip()
        if payload_provider:
            runtime["provider"] = payload_provider
        if payload_model:
            runtime["model"] = payload_model
        if payload_base_url:
            runtime["base_url"] = payload_base_url
        child_env.pop("SCHEDULER_FORCE_GREEDY", None)
        child_env["SCHEDULER_FORCE_AI_ONLY"] = "1"
        _inject_ai_key_env(child_env, api_key)
        _inject_ai_runtime_env(child_env, runtime)
    else:
        child_env["SCHEDULER_FORCE_GREEDY"] = "1"
        child_env.pop("SCHEDULER_FORCE_AI_ONLY", None)

    return {
        "week": week,
        "use_ai": use_ai,
        "child_env": child_env,
    }


def _trainer_profiles_path() -> Path:
    if TRAINER_PROFILES_PATH != DEFAULT_TRAINER_PROFILES_PATH:
        return TRAINER_PROFILES_PATH
    return PROJECT_ROOT / "rules" / "trainer_profiles.json"


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
    path = _trainer_profiles_path()
    if not path.exists():
        return None
    target = " ".join(str(name or "").split()).lower()
    profiles = json.loads(path.read_text())
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

    _write_schedule_data(data)
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
        _schedule_config_path(),
        {"targets": {}, "manual_protected": [], "manual_excluded": [], "custom_rules": []},
    )
    trainer_profiles = load_json_file(_trainer_profiles_path(), [])
    rules_catalog = build_rules_catalog(load_rules_config())
    supabase_upsert("schedule_config", schedule_config)
    supabase_upsert("trainer_profiles", trainer_profiles)
    supabase_upsert("rules_catalog", rules_catalog)
    return {"schedule_config": schedule_config, "trainer_profiles": trainer_profiles, "rules_catalog": rules_catalog}


def pull_supabase_config():
    rows = supabase_request("GET", "/studio_rules?select=config_key,data")
    pulled = {row.get("config_key"): row.get("data") for row in rows if isinstance(row, dict)}
    if pulled.get("schedule_config"):
        path = _schedule_config_path()
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(pulled["schedule_config"], indent=2))
    if pulled.get("trainer_profiles"):
        _trainer_profiles_path().write_text(json.dumps(pulled["trainer_profiles"], indent=2))
    return pulled


# ── GET routes ────────────────────────────────────────────────

@app.route("/")
def index():
    index_path = WEB_DIR / "index.html"
    if index_path.exists():
        return _file(index_path)
    # On a fresh deploy the generated index.html isn't bundled;
    # fall back to the template so the UI still loads.
    template_path = WEB_DIR / "template.html"
    if template_path.exists():
        return _file(template_path)
    return _json({"error": "Schedule UI not yet generated. Run the pipeline first."}, 404)


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
        output_path = _safe_child_path(OUTPUTS_DIR, name)
        if not output_path:
            return _json({"error": f"Not found: {name}"}, 404)
        return _file(output_path)
    candidate = _safe_child_path(WEB_DIR, name)
    if not candidate:
        return _json({"error": f"Not found: {name}"}, 404)
    if candidate.exists() and candidate.is_file():
        return _file(candidate)
    return _json({"error": f"Not found: {name}"}, 404)


@app.route("/api/latest-schedule-file")
def latest_schedule_file():
    global _latest_schedule_file
    _latest_schedule_file = _resolve_latest_schedule_file_name()
    return _json({"file": _latest_schedule_file})


@app.route("/<path:name>")
def static_file(name):
    candidate = _safe_child_path(WEB_DIR, name)
    if not candidate:
        return _json({"error": f"Not found: {name}"}, 404)
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

    payload = request.get_json(silent=True) or {}
    try:
        options = _resolve_pipeline_request_options(payload)
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)
    # Runtime fallback in case payload omitted key but config was just saved.
    if options["use_ai"] and not (
        options["child_env"].get("OPENROUTER_API_KEY") or options["child_env"].get("OPENAI_API_KEY")
    ):
        _inject_ai_key_env(options["child_env"], _saved_ai_api_key())
    if options["use_ai"] and not (
        options["child_env"].get("OPENROUTER_API_KEY") or options["child_env"].get("OPENAI_API_KEY")
    ):
        return _json({
            "ok": False,
            "error": "Add an AI API key in Control Center before using Generate with AI.",
        }, 400)
    print(
        "  [API] run-pipeline mode="
        f"{'ai' if options['use_ai'] else 'standard'} "
        f"openrouter_key={'yes' if bool(options['child_env'].get('OPENROUTER_API_KEY')) else 'no'} "
        f"openai_key={'yes' if bool(options['child_env'].get('OPENAI_API_KEY')) else 'no'} "
        f"openrouter_model={options['child_env'].get('OPENROUTER_MODEL') or '-'} "
        f"openai_model={options['child_env'].get('OPENAI_MODEL') or '-'} "
        f"week={options['week']}"
    )

    _run_counter += 1
    variation_seed = int(_time.time()) % 100000 + _run_counter
    output_suffix = f"run{_run_counter}_{uuid.uuid4().hex[:6]}"
    csv_path = PIPELINE_CSV
    week = options["week"]
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
        _latest_schedule_file = _resolve_latest_schedule_file_name()
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=options["child_env"],
        )
        _pipeline_state["running"] = True
        _pipeline_state["status"] = "running"
        _pipeline_state["pid"] = proc.pid
        _pipeline_state["started"] = _time.time()
        mode_label = "AI planner" if options["use_ai"] else "standard optimiser"
        _pipeline_state["message"] = f"Running {mode_label} — Agent 1: Ingesting data..."

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
                try:
                    global _latest_schedule_file
                    _latest_schedule_file = _resolve_latest_schedule_file_name()
                except Exception:
                    pass
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
            "week_start": week,
            "use_ai": options["use_ai"],
            "message": "Pipeline started. Results ready in ~2 minutes.",
        })
    except Exception as e:
        _pipeline_state["running"] = False
        _pipeline_state["status"] = "failed"
        return _json({"error": str(e)}, 500)


@app.route("/api/chat", methods=["POST"])
def chat():
    try:
        payload = request.get_json(force=True)
        return _json({"reply": _build_chat_reply(payload)})
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    except Exception as exc:
        return _json({"error": str(exc)}, 500)


@app.route("/api/save-trainer-profiles", methods=["POST"])
def save_trainer_profiles():
    try:
        payload = request.get_json(force=True)
        _trainer_profiles_path().write_text(json.dumps(payload, indent=2))
        return _json({"ok": True})
    except Exception as e:
        return _json({"error": str(e)}, 500)


@app.route("/api/save-schedule-config", methods=["POST"])
def save_schedule_config():
    try:
        payload = request.get_json(force=True)
        path = _schedule_config_path()
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))
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
@app.route("/api/clear-schedule/", methods=["POST"])
@app.route("/api/clear-calendar", methods=["POST"])
@app.route("/api/clear-calendar/", methods=["POST"])
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
@app.route("/api/finalize-schedule", methods=["POST"])
def finalise_schedule():
    try:
        return _json({"ok": True, "finalised": _finalise_schedule_to_supabase()})
    except Exception as e:
        return _json({"error": str(e)}, 400)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
def _resolve_latest_schedule_file_name() -> str:
    candidates = sorted(WEB_DIR.glob("schedule_data*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0].name
    return "schedule_data.json"
