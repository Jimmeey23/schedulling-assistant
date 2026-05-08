# AI Schedule Optimizer Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dashboard `Optimize with AI` action that analyzes the active schedule, applies validated class/trainer/time changes, and upgrades the chatbot to a more capable evidence-backed assistant.

**Architecture:** Add a focused AI optimization service that asks the configured model for a JSON patch plan, validates each operation through existing schedule mutation guards, applies safe operations to `web/schedule_data.json`, and returns an audit summary. Keep the dashboard in the current vanilla HTML/JS architecture by adding a new button, status flow, and modern chat drawer states without introducing a frontend framework.

**Tech Stack:** Flask routes in `app.py`, existing OpenAI/OpenRouter-compatible `ai_provider.py`, JSON schedule data in `web/schedule_data.json`, vanilla JS/CSS in `web/template.html`, pytest tests.

---

### Task 1: Backend Optimizer Contract

**Files:**
- Modify: `app.py`
- Test: `tests/test_schedule_quality.py`

- [ ] **Step 1: Write failing tests**

Add tests that monkeypatch the AI response and verify `/api/optimize-schedule` applies validated operations:

```python
def test_optimize_schedule_applies_validated_ai_patch(tmp_path, monkeypatch):
    monkeypatch.setattr(flask_app_module, "WEB_DIR", tmp_path)
    schedule_path = tmp_path / "schedule_data.json"
    schedule_path.write_text(json.dumps({
        "locations": {
            "Supreme HQ, Bandra": [
                {
                    "location": "Supreme HQ, Bandra",
                    "date": "2026-05-04",
                    "day_of_week": "Monday",
                    "time": "09:00",
                    "class_name": "Studio Barre 57",
                    "trainer_1": "Trainer A",
                    "trainer_2": "",
                    "cover": "",
                    "room": "studio_a",
                    "capacity": 14,
                    "duration_min": 57,
                    "score": 20,
                    "predicted_fill_rate": 0.2,
                }
            ]
        }
    }))
    monkeypatch.setattr(flask_app_module, "_call_schedule_optimizer_ai", lambda payload: {
        "summary": "Improve Supreme morning quality.",
        "operations": [
            {
                "type": "swap_trainer",
                "reason": "Trainer B has stronger history.",
                "slot": {
                    "location": "Supreme HQ, Bandra",
                    "day_of_week": "Monday",
                    "time": "09:00",
                    "class_name": "Studio Barre 57",
                    "trainer_1": "Trainer A",
                },
                "new_trainer": "Trainer B",
            }
        ],
    })
    monkeypatch.setattr(flask_app_module, "_validate_manual_slot", lambda data, iteration, slot, original_slot=None: None)
    monkeypatch.setattr(flask_app_module, "_save_schedule_to_supabase", lambda data: {"saved": False})

    response = flask_app_module.app.test_client().post("/api/optimize-schedule", json={"iteration": "Main"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["applied_count"] == 1
    updated = json.loads(schedule_path.read_text())
    assert updated["locations"]["Supreme HQ, Bandra"][0]["trainer_1"] == "Trainer B"
```

- [ ] **Step 2: Run red test**

Run: `pytest tests/test_schedule_quality.py::test_optimize_schedule_applies_validated_ai_patch -q`

Expected: FAIL because `/api/optimize-schedule` does not exist.

- [ ] **Step 3: Implement minimal backend**

Add helpers in `app.py`:

```python
def _call_schedule_optimizer_ai(payload: dict) -> dict:
    ...

def _apply_ai_schedule_operations(data: dict, iteration: str, operations: list[dict]) -> dict:
    ...

@app.route("/api/optimize-schedule", methods=["POST"])
def optimize_schedule():
    ...
```

The apply helper supports `swap_trainer`, `remove_class`, `move_class`, `add_class`, and `change_class`; each operation records `applied`, `rejected`, `reason`, and before/after slot data.

- [ ] **Step 4: Run green test**

Run: `pytest tests/test_schedule_quality.py::test_optimize_schedule_applies_validated_ai_patch -q`

Expected: PASS.

### Task 2: AI Prompt Accuracy

**Files:**
- Modify: `app.py`
- Modify: `chat_assistant.py`
- Test: `tests/test_schedule_quality.py`

- [ ] **Step 1: Write failing tests**

Add tests that verify the optimization prompt includes current schedule rows, allowed operation schema, hard validation language, and active context for chat:

```python
def test_optimizer_prompt_requires_json_patch_and_validation_language(monkeypatch):
    captured = {}
    monkeypatch.setattr(flask_app_module, "_latest_schedule_payload", lambda: {"locations": {"Supreme HQ, Bandra": []}})
    prompt = flask_app_module._build_schedule_optimizer_prompt({"iteration": "Main"})
    assert "Return JSON only" in prompt
    assert "swap_trainer" in prompt
    assert "remove_class" in prompt
    assert "move_class" in prompt
    assert "add_class" in prompt
    assert "Every operation will be server-validated" in prompt

def test_chat_context_includes_active_dashboard_context(tmp_path):
    schedule = tmp_path / "schedule_data.json"
    scorecard = tmp_path / "scorecard.json"
    profiles = tmp_path / "profiles.json"
    schedule.write_text(json.dumps({"locations": {"Supreme HQ, Bandra": [{"day_of_week": "Monday", "time": "09:00", "class_name": "Studio Barre 57", "trainer_1": "Trainer A"}]}}))
    scorecard.write_text(json.dumps({}))
    profiles.write_text(json.dumps([]))
    context = build_chat_context(schedule, scorecard, profiles, "optimize supreme", dashboard_context={"location": "Supreme HQ, Bandra", "mode": "Analyze"})
    assert "ACTIVE DASHBOARD CONTEXT" in context
    assert "Supreme HQ, Bandra" in context
```

- [ ] **Step 2: Run red tests**

Run: `pytest tests/test_schedule_quality.py -q -k 'optimizer_prompt_requires_json_patch or chat_context_includes_active_dashboard_context'`

Expected: FAIL because helpers/context parameter do not exist.

- [ ] **Step 3: Implement prompt/context**

Add `_build_schedule_optimizer_prompt(payload)` and let `build_chat_context(..., dashboard_context=None)` append active location, iteration, mode, selected slot, and visible filters.

- [ ] **Step 4: Run green tests**

Run: `pytest tests/test_schedule_quality.py -q -k 'optimizer_prompt_requires_json_patch or chat_context_includes_active_dashboard_context'`

Expected: PASS.

### Task 3: Dashboard Button and Modern Chat UI

**Files:**
- Modify: `web/template.html`
- Test: `tests/test_schedule_quality.py`

- [ ] **Step 1: Write failing tests**

Add string-level template tests:

```python
def test_dashboard_has_ai_optimize_button_and_client_handler():
    template = Path("web/template.html").read_text()
    assert 'id="optimize-ai-btn"' in template
    assert "function optimizeScheduleWithAI" in template
    assert 'fetch("/api/optimize-schedule"' in template

def test_chat_ui_has_advanced_modes_and_context_payload():
    template = Path("web/template.html").read_text()
    assert "chat-mode-tabs" in template
    assert "data-chat-mode" in template
    assert "dashboard_context" in template
    assert "Analyze" in template
    assert "Optimize Ideas" in template
```

- [ ] **Step 2: Run red tests**

Run: `pytest tests/test_schedule_quality.py -q -k 'dashboard_has_ai_optimize_button or chat_ui_has_advanced_modes'`

Expected: FAIL.

- [ ] **Step 3: Implement UI**

Add a button next to Generate controls, a loading state, a success toast with applied/rejected counts, and a chat drawer with mode tabs, stronger empty state, and richer request payload.

- [ ] **Step 4: Run green tests**

Run: `pytest tests/test_schedule_quality.py -q -k 'dashboard_has_ai_optimize_button or chat_ui_has_advanced_modes'`

Expected: PASS.

### Task 4: Verification

**Files:**
- Verify: `app.py`
- Verify: `web/template.html`
- Verify: `tests/test_schedule_quality.py`

- [ ] **Step 1: Run backend/frontend template tests**

Run: `pytest tests/test_schedule_quality.py -q -k 'optimize_schedule or optimizer_prompt or chat_context or dashboard_has_ai_optimize_button or chat_ui_has_advanced_modes'`

Expected: PASS.

- [ ] **Step 2: Run broader non-template scheduling tests**

Run: `pytest tests/test_constraints.py tests/test_schedule_quality.py -q -k 'not web_template'`

Expected: PASS.

- [ ] **Step 3: Start local server**

Run: `python3 app.py`

Expected: Flask starts on `http://localhost:5000`.

- [ ] **Step 4: Render check**

Open dashboard, confirm `Optimize with AI` is visible, chat drawer opens, mode tabs switch, and the first viewport has no overlapping controls.

---

## Self-Review

- Spec coverage: backend optimizer route, validated schedule operations, dashboard button, richer chatbot context, modern chat UI, and tests are covered.
- Placeholder scan: no `TBD`, `TODO`, or unspecified code steps.
- Type consistency: operation types use the same names in prompt, tests, route, and UI.
