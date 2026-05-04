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

from rule_config import build_rules_catalog, load_rules_config, update_rules_config

PROJECT_ROOT = Path(__file__).parent
WEB_DIR = PROJECT_ROOT / "web"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
SCHEDULE_CONFIG_PATH = PROJECT_ROOT / "config" / "schedule_config.json"
TRAINER_PROFILES_PATH = PROJECT_ROOT / "rules" / "trainer_profiles.json"


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
_pipeline_state = {"running": False, "pid": None, "started": None, "message": "Idle"}
_run_counter = 0
_latest_schedule_file = "schedule_data.json"

# ── Flask app ─────────────────────────────────────────────────

app = Flask(__name__, static_folder=None)


def _json(data, status=200):
    return Response(json.dumps(data), status=status, mimetype="application/json")


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
    msg = _pipeline_state["message"]
    running = _pipeline_state["running"]
    if running:
        status = "running"
    elif "Complete" in msg or "complete" in msg:
        status = "done"
    elif "Failed" in msg or "failed" in msg or "Error" in msg:
        status = "failed"
    else:
        status = "idle"
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
                state["message"] = "Complete — reload to see new schedule."
            else:
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
