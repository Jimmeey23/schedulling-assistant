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

from flask import Flask, Response, request

from rule_config import build_rules_catalog, load_rules_config, update_rules_config

PROJECT_ROOT = Path(__file__).parent
WEB_DIR = PROJECT_ROOT / "web"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"

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

# ── Flask app ─────────────────────────────────────────────────

app = Flask(__name__, static_folder=None)


def _json(data, status=200):
    return Response(json.dumps(data), status=status, mimetype="application/json")


def _file(path: Path):
    if not path.exists():
        return _json({"error": f"Not found: {path.name}"}, 404)
    mime = MIME.get(path.suffix.lower(), "application/octet-stream")
    return Response(path.read_bytes(), mimetype=mime)


# ── GET routes ────────────────────────────────────────────────

@app.route("/")
def index():
    return _file(WEB_DIR / "index.html")


@app.route("/api/rules-config")
def rules_config():
    return _json(build_rules_catalog(load_rules_config()))


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
    return _json({"targets": {}, "manual_protected": [], "manual_excluded": []})


@app.route("/schedule_<path:fname>")
@app.route("/ai_<path:fname>")
@app.route("/scorecard<path:fname>")
def output_file(fname=""):
    name = request.path.lstrip("/")
    return _file(OUTPUTS_DIR / name)


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
            stages = [
                "Running — Agent 1: Ingesting data...",
                "Running — Agent 2: Analysing history...",
                "Running — Agent 3: Scoring slots...",
                "Running — Agent 4: Applying rules...",
                "Running — Agent 5: AI planning...",
                "Running — Agent 6: Building report...",
            ]
            idx = 0
            while p.poll() is None:
                _time.sleep(15)
                idx = min(idx + 1, len(stages) - 1)
                state["message"] = stages[idx]
            if p.returncode == 0:
                state["message"] = "Complete — reload to see new schedule."
            else:
                state["message"] = f"Failed (exit {p.returncode}). Check server logs."
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
