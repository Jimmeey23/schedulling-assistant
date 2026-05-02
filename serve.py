"""
serve.py — Local HTTP server for Studio Scheduler web interface.
Serves the generated schedule web UI and exposes API endpoints for rule toggling.

Usage:
    python3 serve.py --week 2026-05-04 --port 8080
"""
import argparse
import json
import subprocess
import sys
import threading
import time as _time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from rule_config import build_rules_catalog, load_rules_config, update_rules_config

PROJECT_ROOT = Path(__file__).parent
WEB_DIR = PROJECT_ROOT / "web"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
CONFIG_PATH = PROJECT_ROOT / "config" / "rules_config.json"

# Pipeline process state
_pipeline_state = {"running": False, "pid": None, "started": None, "message": "Idle"}

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

        # API: pipeline status
        if path == "/api/pipeline-status":
            self._send_json(200, {
                "running": _pipeline_state["running"],
                "pid": _pipeline_state["pid"],
                "started": _pipeline_state["started"],
                "message": _pipeline_state["message"],
            })
            return

        # Serve root → web/index.html
        if path == "/":
            self._serve_file(WEB_DIR / "index.html")
            return

        # Serve outputs/ files (schedule CSVs, XLSXs, JSON)
        if path.startswith("/schedule_") or path.startswith("/ai_") or path.startswith("/scorecard"):
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

        if path == "/api/run-pipeline":
            if _pipeline_state["running"]:
                self._send_json(200, {
                    "ok": True,
                    "already_running": True,
                    "message": "Pipeline is already running. Please wait.",
                })
                return
            csv_path = self.pipeline_csv
            week = self.pipeline_week
            cmd = [
                sys.executable, str(PROJECT_ROOT / "orchestrator.py"),
                "--csv", csv_path,
                "--week", week,
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

                self._send_json(200, {
                    "ok": True,
                    "pid": proc.pid,
                    "message": "Pipeline started. Results ready in ~2 minutes.",
                })
            except Exception as e:
                _pipeline_state["running"] = False
                self._send_json(500, {"error": str(e)})
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
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    parser.add_argument("--week", default="2026-05-04", help="Default week for pipeline re-run")
    parser.add_argument("--csv", default="Class Performance by Trainer.csv",
                        help="CSV path for pipeline re-run")
    args = parser.parse_args()

    RulesHandler.pipeline_week = args.week
    RulesHandler.pipeline_csv = args.csv

    server = HTTPServer(("", args.port), RulesHandler)
    print(f"\n  Studio Scheduler — serving at http://localhost:{args.port}")
    print(f"  Pipeline week: {args.week} | CSV: {args.csv}")
    print(f"  Rule toggles: http://localhost:{args.port}  (⚙ Rules panel)")
    print(f"  Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")


if __name__ == "__main__":
    main()
