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
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-oss-120b:free"
DEFAULT_OPENROUTER_BACKUP_MODEL = "z-ai/glm-4.5-air:free"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


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

# Pipeline state (synced to disk for multi-worker environments)
STATE_DIR = PROJECT_ROOT / "state"
PIPELINE_STATE_FILE = STATE_DIR / "pipeline_status.json"

def _read_pipeline_state() -> dict:
    default_state = {
        "running": False,
        "status": "idle",
        "pid": None,
        "started": None,
        "message": "Idle",
    }
    try:
        if PIPELINE_STATE_FILE.exists():
            with open(PIPELINE_STATE_FILE) as f:
                data = json.load(f)
                return {**default_state, **data}
    except Exception:
        pass
    return default_state

def _write_pipeline_state(state: dict) -> None:
    try:
        PIPELINE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PIPELINE_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass
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

def _write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


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
        from ai_provider import load_dotenv_if_present
        load_dotenv_if_present()
    except Exception:
        pass
    env_key = (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if env_key:
        return env_key
    try:
        data = json.loads(_schedule_config_path().read_text())
        settings = data.get("settings_options") or {}
        cfg_key = str(settings.get("ai_api_key") or "").strip()
        if cfg_key:
            return cfg_key
    except Exception:
        pass
    return ""


def _saved_deepseek_api_key() -> str:
    try:
        from ai_provider import load_dotenv_if_present
        load_dotenv_if_present()
    except Exception:
        pass
    env_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env_key:
        return env_key
    try:
        data = json.loads(_schedule_config_path().read_text())
        settings = data.get("settings_options") or {}
        cfg_key = str(settings.get("deepseek_api_key") or "").strip()
        if cfg_key:
            return cfg_key
    except Exception:
        pass
    return ""

def _saved_ai_runtime_settings() -> dict:
    try:
        data = json.loads(_schedule_config_path().read_text())
    except Exception:
        return {}
    settings = data.get("settings_options") or {}
    return {
        "provider": str(settings.get("ai_provider") or "deepseek").strip().lower(),
        "model": str(settings.get("ai_model") or DEFAULT_OPENROUTER_MODEL).strip(),
        "backup_model": str(settings.get("ai_backup_model") or DEFAULT_OPENROUTER_BACKUP_MODEL).strip(),
        "base_url": str(settings.get("ai_base_url") or "").strip(),
        "deepseek_model": str(settings.get("deepseek_model") or DEFAULT_DEEPSEEK_MODEL).strip(),
        "deepseek_base_url": str(settings.get("deepseek_base_url") or DEFAULT_DEEPSEEK_BASE_URL).strip(),
        "deepseek_api_key": str(settings.get("deepseek_api_key") or "").strip(),
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
        runtime = _saved_ai_runtime_settings()
        provider = runtime.get("provider") or "deepseek"
        api_key = _saved_deepseek_api_key() if provider == "deepseek" else _saved_ai_api_key()
        if not api_key and provider == "deepseek":
            provider = "openrouter"
            api_key = _saved_ai_api_key()
        if api_key:
            try:
                from openai import OpenAI
                if provider == "deepseek":
                    base_url = runtime.get("deepseek_base_url") or DEFAULT_DEEPSEEK_BASE_URL
                    model = runtime.get("deepseek_model") or DEFAULT_DEEPSEEK_MODEL
                else:
                    base_url = runtime.get("base_url") or (
                        DEFAULT_OPENAI_BASE_URL if provider == "openai" else DEFAULT_OPENROUTER_BASE_URL
                    )
                    model = runtime.get("model") or (
                        os.environ.get("OPENAI_MODEL") if provider == "openai" else os.environ.get("OPENROUTER_MODEL")
                    ) or DEFAULT_OPENROUTER_MODEL
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
        return "AI not configured. Add a DeepSeek API key in Control Center or set DEEPSEEK_API_KEY."

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

def _inject_ai_key_env(child_env: dict, api_key: str, provider: str = "openrouter") -> None:
    key = str(api_key or "").strip()
    if not key:
        return
    provider = str(provider or "openrouter").strip().lower()
    if provider == "deepseek":
        child_env["DEEPSEEK_API_KEY"] = key
    elif provider == "openai":
        child_env["OPENAI_API_KEY"] = key
    else:
        child_env["OPENROUTER_API_KEY"] = key

def _inject_ai_runtime_env(child_env: dict, runtime: dict) -> None:
    provider = str(runtime.get("provider") or "openrouter").strip().lower()
    model = str(runtime.get("model") or "").strip()
    backup_model = str(runtime.get("backup_model") or "").strip()
    base_url = str(runtime.get("base_url") or "").strip()
    deepseek_model = str(runtime.get("deepseek_model") or DEFAULT_DEEPSEEK_MODEL).strip()
    deepseek_base_url = str(runtime.get("deepseek_base_url") or DEFAULT_DEEPSEEK_BASE_URL).strip()
    if backup_model:
        child_env["AI_BACKUP_MODEL"] = backup_model
        child_env["OPENROUTER_BACKUP_MODEL"] = backup_model
    if provider == "deepseek":
        child_env["DEEPSEEK_MODEL"] = deepseek_model or DEFAULT_DEEPSEEK_MODEL
        child_env["DEEPSEEK_BASE_URL"] = deepseek_base_url or DEFAULT_DEEPSEEK_BASE_URL
        if model:
            child_env["OPENROUTER_MODEL"] = model
        if base_url:
            child_env["OPENROUTER_BASE_URL"] = base_url
        elif model:
            child_env["OPENROUTER_BASE_URL"] = DEFAULT_OPENROUTER_BASE_URL
    elif provider == "openai":
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
    child_env["PYTHONUNBUFFERED"] = "1"

    if use_ai:
        api_key = _saved_ai_api_key() or str(payload.get("api_key") or "").strip()
        deepseek_api_key = (
            _saved_deepseek_api_key()
            or str(payload.get("deepseek_api_key") or "").strip()
        )
        runtime = _saved_ai_runtime_settings()
        payload_provider = str(payload.get("ai_provider") or "").strip().lower()
        payload_model = str(payload.get("ai_model") or "").strip()
        payload_base_url = str(payload.get("ai_base_url") or "").strip()
        payload_backup_model = str(payload.get("ai_backup_model") or "").strip()
        payload_deepseek_model = str(payload.get("deepseek_model") or "").strip()
        payload_deepseek_base_url = str(payload.get("deepseek_base_url") or "").strip()
        if payload_provider:
            runtime["provider"] = payload_provider
        elif deepseek_api_key:
            runtime["provider"] = "deepseek"
        if payload_model:
            runtime["model"] = payload_model
        if payload_base_url:
            runtime["base_url"] = payload_base_url
        if payload_backup_model:
            runtime["backup_model"] = payload_backup_model
        if payload_deepseek_model:
            runtime["deepseek_model"] = payload_deepseek_model
        if payload_deepseek_base_url:
            runtime["deepseek_base_url"] = payload_deepseek_base_url
        if deepseek_api_key:
            runtime["deepseek_api_key"] = deepseek_api_key
        child_env.pop("SCHEDULER_FORCE_GREEDY", None)
        child_env["SCHEDULER_FORCE_AI_ONLY"] = "1"
        if runtime.get("provider") == "deepseek":
            if deepseek_api_key:
                _inject_ai_key_env(child_env, deepseek_api_key, "deepseek")
                _inject_ai_key_env(child_env, api_key, "openrouter")
            else:
                runtime["provider"] = "openrouter"
                _inject_ai_key_env(child_env, api_key, "openrouter")
        else:
            _inject_ai_key_env(child_env, api_key, runtime.get("provider") or "openrouter")
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
    external = state is not None
    if not external:
        state = _read_pipeline_state()
    if not state.get("running"):
        return
    if _pipeline_process_alive(state.get("pid")):
        return
    if not external:
        # Process is dead. Give monitor thread a chance to write its state before assuming failure.
        _time.sleep(1.0)
        state = _read_pipeline_state()
        if not state.get("running"):
            return
    state["running"] = False
    state["status"] = "failed"
    state["pid"] = None
    state["message"] = "Previous pipeline process stopped before completion. Generate can be run again."
    if not external:
        _write_pipeline_state(state)


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


def _manual_class_base(slot):
    if slot.get("is_private_session") and slot.get("private_session_format"):
        return slot.get("private_session_format") or ""
    return slot.get("class_name") or ""


def _qual_key_for_manual_class(class_name):
    lower = str(class_name or "").lower()
    if "powercycle" in lower or "power cycle" in lower:
        return "powercycle"
    if "strength lab" in lower:
        return "strength_lab"
    if "fit" in lower:
        return "fit"
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
    if "back body blaze" in lower:
        return "back_body_blaze"
    if "cardio barre" in lower:
        return "cardio_barre"
    return "all_barre"


def _trainer_qualified_for_manual_class(profile, class_name):
    q = profile.get("qualifications") or {}
    if not any(bool(v) for v in q.values()):
        return True
    key = _qual_key_for_manual_class(class_name)
    if q.get(key):
        return True
    lower = str(class_name or "").lower()
    if "express" in lower and key == "powercycle" and q.get("express_cycle"):
        return True
    if "express" in lower and key in {"all_barre", "cardio_barre", "mat_57"} and q.get("express_barre"):
        return True
    return False


def _manual_class_allowed_at_location(slot):
    loc = slot.get("location") or ""
    class_name = _manual_class_base(slot)
    if not class_name:
        return True
    if "private session" in str(slot.get("class_name") or "").lower() and not slot.get("private_session_format"):
        return True
    if "PowerCycle" in class_name and loc not in MUMBAI_LOCATIONS:
        raise ValueError(f"{class_name} is not enabled for {loc}")
    if "Strength Lab" in class_name and loc != "Kwality House, Kemps Corner":
        raise ValueError(f"{class_name} is not enabled for {loc}")
    cfg_path = PROJECT_ROOT / "config" / "schedule_config.json"
    try:
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        entry = ((cfg.get("class_mix") or {}).get(loc) or {}).get(class_name) or {}
        if int(entry.get("min", 0) or 0) == 0 and int(entry.get("max", 1) or 0) == 0:
            raise ValueError(f"{class_name} is disabled for {loc} in class mix settings")
    except ValueError:
        raise
    except Exception:
        pass


def _room_overlaps(slot, rows):
    room = (slot.get("room") or "").strip()
    if not room:
        return None
    for r in rows:
        if (r.get("location") or "") != (slot.get("location") or ""):
            continue
        if (r.get("day_of_week") or "") != (slot.get("day_of_week") or ""):
            continue
        if (r.get("room") or "").strip() != room:
            continue
        if _slot_overlap(slot, r):
            return r
    return None


def _validate_manual_slot(data, iteration, slot, original_slot=None, additional_rows=None):
    trainer = slot.get("trainer_1") or ""
    profile = _trainer_profile(trainer)
    if not profile:
        raise ValueError(f"No active trainer profile found for {trainer}")
    if profile.get("active") is False:
        raise ValueError(f"{trainer} is inactive")
    _manual_class_allowed_at_location(slot)
    base_class = _manual_class_base(slot)
    if base_class and "private session" not in base_class.lower() and not _trainer_qualified_for_manual_class(profile, base_class):
        raise ValueError(f"{trainer} is not qualified for {base_class}")
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
    rows.extend(additional_rows or [])
    rows = [r for r in rows if not (original_slot and _same_schedule_slot(r, original_slot))]
    trainer_rows = [r for r in rows if (r.get("trainer_1") or "").strip().lower() == trainer.strip().lower()]
    room_conflict = _room_overlaps(slot, rows)
    if room_conflict:
        raise ValueError(
            f"{slot.get('room')} is already occupied at {slot.get('location')} on "
            f"{slot.get('day_of_week')} {slot.get('time')}"
        )

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


def _add_classes_to_schedule(payload):
    slots = payload.get("slots") or []
    iteration = payload.get("iteration") or "Main"
    if not isinstance(slots, list) or not slots:
        raise ValueError("Missing slots")

    path = WEB_DIR / "schedule_data.json"
    if not path.exists():
        raise FileNotFoundError("web/schedule_data.json was not found")
    data = json.loads(path.read_text())

    prepared = []
    pending = []
    for raw in slots:
        slot = dict(raw or {})
        required = ("location", "day_of_week", "time", "class_name", "trainer_1")
        missing = [k for k in required if not slot.get(k)]
        if missing:
            raise ValueError(f"Missing slot field(s): {', '.join(missing)}")
        slot.setdefault("recommendation", "MANUAL")
        slot.setdefault("manual_added", True)
        slot.setdefault("scheduling_reason", "Manual recurring class added from calendar")
        _validate_manual_slot(data, iteration, slot, additional_rows=pending)
        prepared.append(slot)
        pending.append(slot)

    for slot in prepared:
        loc = slot["location"]
        if iteration == "Main":
            data.setdefault("locations", {}).setdefault(loc, []).append(slot)
        else:
            data.setdefault("iterations", {}).setdefault(iteration, {}).setdefault(loc, []).append(slot)

    supabase_saved = _write_schedule_data(data)
    return {"added": len(prepared), "supabase_saved": supabase_saved}


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
    state = _read_pipeline_state()
    msg = state.get("message", "Idle")
    running = state.get("running", False)
    status = state.get("status", "running" if running else "idle")
    return _json({
        "running": running,
        "status": status,
        "pid": state.get("pid"),
        "started": state.get("started"),
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


def _latest_schedule_payload():
    schedule_path = WEB_DIR / "schedule_data.json"
    if not schedule_path.exists():
        return {"locations": {}}
    with open(schedule_path) as f:
        return json.load(f)


def _build_schedule_optimizer_prompt(payload):
    location = payload.get("location", "")
    schedule = _latest_schedule_payload()
    loc_slots = schedule.get("locations", {}).get(location, []) if location else []
    return (
        "You are a studio schedule optimizer. Review this schedule and suggest specific improvements. "
        f"Location: {location}\n\n"
        f"Schedule:\n{json.dumps(loc_slots, indent=2)}\n\n"
        "Return JSON only with this exact structure:\n"
        '{"summary": "brief summary", "operations": [...]}\n\n'
        "Supported operation types: swap_trainer, remove_class, move_class, add_class, change_class\n\n"
        "Every operation will be server-validated before being applied. "
        "Each operation must include a 'slot' object identifying the target slot, "
        "and 'reason' explaining the change."
    )


def _call_schedule_optimizer_ai(payload):
    prompt = _build_schedule_optimizer_prompt(payload)
    import ai_provider as _aip
    try:
        runtime = _saved_ai_runtime_settings()
        provider = str(payload.get("ai_provider") or runtime.get("provider") or "deepseek").strip().lower()
        if provider == "openai":
            model = str(payload.get("ai_model") or runtime.get("model") or "gpt-4o-mini").strip()
            base_url = str(payload.get("ai_base_url") or runtime.get("base_url") or DEFAULT_OPENAI_BASE_URL).strip()
        elif provider == "openrouter":
            model = str(payload.get("ai_model") or runtime.get("model") or DEFAULT_OPENROUTER_MODEL).strip()
            base_url = str(payload.get("ai_base_url") or runtime.get("base_url") or DEFAULT_OPENROUTER_BASE_URL).strip()
        else:
            provider = "deepseek"
            model = str(payload.get("deepseek_model") or runtime.get("deepseek_model") or DEFAULT_DEEPSEEK_MODEL).strip()
            base_url = str(payload.get("deepseek_base_url") or runtime.get("deepseek_base_url") or DEFAULT_DEEPSEEK_BASE_URL).strip()
        api_key = (
            str(payload.get("api_key") or "").strip()
            or (str(_saved_deepseek_api_key() or "").strip() if provider == "deepseek" else str(_saved_ai_api_key() or "").strip())
        )
        raw = _aip.call_ai(
            prompt=prompt,
            max_tokens=2000,
            api_key=api_key or None,
            provider=provider,
            model=model,
            base_url=base_url,
        )
        import re
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as exc:
        print(f"  [API] optimize-schedule AI failed: {exc}")
    return {"summary": "AI optimization unavailable.", "operations": []}


@app.route("/api/optimize-schedule", methods=["POST"])
def optimize_schedule():
    try:
        payload = request.get_json(force=True) or {}
        iteration = payload.get("iteration", "Main")

        schedule_path = WEB_DIR / "schedule_data.json"
        if not schedule_path.exists():
            return _json({"error": "No schedule data found"}, 404)

        with open(schedule_path) as f:
            data = json.load(f)

        result = _call_schedule_optimizer_ai(payload)
        operations = result.get("operations", []) or []
        summary = result.get("summary", "")

        applied = 0
        rejected = 0

        for op in operations:
            op_type = op.get("type", "")
            slot_spec = op.get("slot") or {}
            target_location = slot_spec.get("location") or payload.get("location", "")
            if not target_location:
                rejected += 1
                continue

            loc_rows = data.get("locations", {}).get(target_location) or []
            target_slot = None
            for row in loc_rows:
                if (
                    row.get("location") == slot_spec.get("location")
                    and row.get("day_of_week") == slot_spec.get("day_of_week")
                    and row.get("time") == slot_spec.get("time")
                    and row.get("class_name") == slot_spec.get("class_name")
                    and row.get("trainer_1") == slot_spec.get("trainer_1")
                ):
                    target_slot = row
                    break

            if target_slot is None:
                rejected += 1
                continue

            new_slot = {**target_slot}
            if op_type == "swap_trainer":
                new_slot["trainer_1"] = op.get("new_trainer", new_slot["trainer_1"])
            elif op_type == "change_class":
                new_slot["class_name"] = op.get("new_class", new_slot["class_name"])
            elif op_type == "move_class":
                new_slot["time"] = op.get("new_time", new_slot["time"])
            elif op_type == "remove_class":
                pass
            else:
                rejected += 1
                continue

            try:
                _validate_manual_slot(data, iteration, new_slot, original_slot=target_slot)
            except (ValueError, Exception):
                rejected += 1
                continue

            idx = loc_rows.index(target_slot)
            if op_type == "remove_class":
                loc_rows.pop(idx)
            else:
                new_slot["ai_optimized"] = True
                loc_rows[idx] = new_slot
            applied += 1

        if applied > 0:
            _write_json_atomic(schedule_path, data)
            _regenerate_index_from_template(data)
            _save_schedule_to_supabase(data)

        return _json({
            "ok": True,
            "applied_count": applied,
            "rejected_count": rejected,
            "summary": summary,
            "operations": operations,
        })
    except Exception as e:
        return _json({"error": str(e)}, 500)

@app.route("/api/run-pipeline", methods=["POST"])
def run_pipeline():
    global _run_counter
    _refresh_pipeline_state()
    state = _read_pipeline_state()
    if state.get("running"):
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
        options["child_env"].get("DEEPSEEK_API_KEY")
        or options["child_env"].get("OPENROUTER_API_KEY")
        or options["child_env"].get("OPENAI_API_KEY")
    ):
        deepseek_key = _saved_deepseek_api_key()
        if deepseek_key:
            _inject_ai_key_env(options["child_env"], deepseek_key, "deepseek")
        else:
            _inject_ai_key_env(options["child_env"], _saved_ai_api_key())
    if options["use_ai"] and not (
        options["child_env"].get("DEEPSEEK_API_KEY")
        or options["child_env"].get("OPENROUTER_API_KEY")
        or options["child_env"].get("OPENAI_API_KEY")
    ):
        return _json({
            "ok": False,
            "error": "Add a DeepSeek API key in Control Center before using Generate with AI.",
        }, 400)
    print(
        "  [API] run-pipeline mode="
        f"{'ai' if options['use_ai'] else 'standard'} "
        f"deepseek_key={'yes' if bool(options['child_env'].get('DEEPSEEK_API_KEY')) else 'no'} "
        f"openrouter_key={'yes' if bool(options['child_env'].get('OPENROUTER_API_KEY')) else 'no'} "
        f"openai_key={'yes' if bool(options['child_env'].get('OPENAI_API_KEY')) else 'no'} "
        f"deepseek_model={options['child_env'].get('DEEPSEEK_MODEL') or '-'} "
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
        state = _read_pipeline_state()
        state["running"] = True
        state["status"] = "running"
        state["pid"] = proc.pid
        state["started"] = _time.time()
        mode_label = "AI planner" if options["use_ai"] else "standard optimiser"
        state["message"] = f"Running {mode_label} — Agent 1: Ingesting data..."
        _write_pipeline_state(state)

        def _monitor(p):
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
                s = _read_pipeline_state()
                if "[Agent 5]" in clean:
                    inner = clean.split("[Agent 5]", 1)[-1].strip()
                    s["message"] = f"Optimising schedule — {inner}"
                    _write_pipeline_state(s)
                    continue
                for marker, label in stage_prefixes:
                    if marker in clean:
                        s["message"] = f"Running — {label}..."
                        _write_pipeline_state(s)
                        break
            p.wait()
            s = _read_pipeline_state()
            if p.returncode == 0:
                s["status"] = "done"
                s["message"] = "Complete — reload to see new schedule."
                try:
                    global _latest_schedule_file
                    _latest_schedule_file = _resolve_latest_schedule_file_name()
                except Exception:
                    pass
            else:
                s["status"] = "failed"
                detail = next((item for item in reversed(tail) if item.strip()), "")
                s["message"] = f"Failed (exit {p.returncode}). {detail or 'Check server logs.'}"
            s["running"] = False
            s["pid"] = None
            _write_pipeline_state(s)

        t = threading.Thread(target=_monitor, args=(proc,), daemon=True)
        t.start()

        return _json({
            "ok": True,
            "pid": proc.pid,
            "week_start": week,
            "use_ai": options["use_ai"],
            "message": "Pipeline started. Results ready in ~2 minutes.",
        })
    except Exception as e:
        s = _read_pipeline_state()
        s["running"] = False
        s["status"] = "failed"
        _write_pipeline_state(s)
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
        _write_json_atomic(_trainer_profiles_path(), payload)
        return _json({"ok": True})
    except Exception as e:
        return _json({"error": str(e)}, 500)


@app.route("/api/save-schedule-config", methods=["POST"])
def save_schedule_config():
    try:
        payload = request.get_json(force=True)
        _write_json_atomic(_schedule_config_path(), payload)
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


@app.route("/api/add-classes", methods=["POST"])
def add_classes():
    try:
        payload = request.get_json(force=True)
        result = _add_classes_to_schedule(payload)
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


@app.errorhandler(404)
def resource_not_found(e):
    return _json({"error": "Resource not found"}, 404)

@app.errorhandler(405)
def method_not_allowed(e):
    return _json({"error": "Method not allowed"}, 405)

@app.errorhandler(Exception)
def handle_exception(e):
    # Pass through HTTP errors
    if hasattr(e, 'code') and isinstance(e.code, int):
        return _json({"error": str(e)}, e.code)
    # Log and return 500 for unhandled exceptions
    print(f"Unhandled exception: {e}", file=sys.stderr)
    return _json({"error": "Internal server error"}, 500)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
def _resolve_latest_schedule_file_name() -> str:
    candidates = sorted(WEB_DIR.glob("schedule_data*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0].name
    return "schedule_data.json"
