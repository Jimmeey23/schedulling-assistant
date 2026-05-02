"""
app.py — Flask WSGI entrypoint for Studio Scheduler.
Replaces serve.py's raw HTTP server for autoscale deployment.
The Generate pipeline is only available when running locally.
"""
import json
import os
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file

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
    return _json({"running": False, "status": "idle", "message": "Idle"})


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
    return _json({
        "ok": False,
        "error": "Schedule generation is only available when running the app locally.",
    }, 503)


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
