# Custom Rules and Manual Pins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add dropdown-guided custom rule and manual pinned-class controls to Settings, and make manual pins hard scheduler locks.

**Architecture:** Reuse `config/schedule_config.json` and `/api/save-schedule-config`. Store custom rules in `custom_rules` and manual pins in `manual_protected`. UI renders settings tabs and writes structured JSON; optimizer consumes `manual_protected` through `_get_pinned_slots()` before generated pins.

**Tech Stack:** Python optimizer/tests, vanilla HTML/CSS/JS in `web/template.html`, existing reporter/template generation pipeline.

---

### Task 1: Regression Tests

**Files:**
- Modify: `tests/test_schedule_quality.py`
- Modify: `tests/test_replace_trainer_modal.py`

- [ ] Add optimizer test asserting `manual_protected` entries appear in `_get_pinned_slots()`.
- [ ] Add template tests asserting Settings exposes `stab-customrules`, `ssec-customrules`, `settRenderCustomRules`, `settAddManualPin`, and `manual_protected` save paths.
- [ ] Run targeted tests and verify they fail before implementation.

### Task 2: Optimizer Manual Pins

**Files:**
- Modify: `agents/optimiser.py`

- [ ] Add helper to normalize schedule config manual pins.
- [ ] Prepend matching `manual_protected` entries in `_get_pinned_slots(location, day_name)`.
- [ ] Preserve optional `room`, `id`, and `note` metadata for future UI display.
- [ ] Run optimizer tests.

### Task 3: Settings UI

**Files:**
- Modify: `web/template.html`

- [ ] Add Settings tab `Custom Rules & Pins`.
- [ ] Add two sections: dropdown custom-rule builder and pinned-class builder.
- [ ] Implement render/add/remove/save functions for `_settSchedConfig.custom_rules` and `_settSchedConfig.manual_protected`.
- [ ] Use existing constants `LOCS_ALL`, `DAYS_ALL`, `CLASS_MIX_TARGETS`, and trainer profiles for dropdown options.
- [ ] Run template tests and JS syntax test.

### Task 4: Verify End to End

**Files:**
- Generated: `web/index.html`

- [ ] Run the template/report generation path or full pipeline to regenerate `web/index.html`.
- [ ] Run `pytest -q`.
- [ ] Run `python3 -m compileall -q app.py serve.py orchestrator.py ai_provider.py agents tests rule_config.py`.
