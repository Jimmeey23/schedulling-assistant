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
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

from rule_config import build_rules_catalog, load_rules_config, update_rules_config

PROJECT_ROOT = Path(__file__).parent
WEB_DIR = PROJECT_ROOT / "web"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
CONFIG_PATH = PROJECT_ROOT / "config" / "rules_config.json"
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

# Pipeline process state
_pipeline_state = {"running": False, "pid": None, "started": None, "message": "Idle"}
_run_counter = 0

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


def supabase_settings() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
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
                        state["message"] = "Complete — reload to see new schedule."
                    else:
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
