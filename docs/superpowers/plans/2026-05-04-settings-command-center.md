# Settings Command Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the Settings tab into a Command Center with canonical config editing, conflict visibility, and bulk row/column/selection operators.

**Architecture:** Keep the existing static template architecture. Add reusable client-side helpers inside `web/template.html` to render the new shell, validate `_settSchedConfig`, apply bulk operators to matrix-based settings, and save the same config object through `/api/save-schedule-config`. Regenerate `web/index.html` through the reporter after template changes.

**Tech Stack:** Vanilla HTML/CSS/JS in `web/template.html`, Flask/raw-server JSON APIs, pytest for focused API regression coverage.

---

### Task 1: Protect Schedule Config Save Shape

**Files:**
- Modify: `tests/test_schedule_quality.py`
- Verify existing API in: `app.py`, `serve.py`

- [ ] Add a focused test that posts a schedule config containing targets, class mix, custom rules, and source metadata to `/api/save-schedule-config`, then verifies the JSON is persisted exactly.
- [ ] Run the new test and confirm it fails only if the endpoint drops unknown canonical settings metadata.
- [ ] Keep endpoint implementation unchanged if it already persists the full config object.

### Task 2: Build Command Center Shell

**Files:**
- Modify: `web/template.html`

- [ ] Replace the existing horizontal settings tabs with a three-column Command Center: left navigation/status rail, central content panel, right inspector/action rail.
- [ ] Add visual source-of-truth status, save/validate actions, change count, and conflict summary.
- [ ] Preserve all existing settings sections: trainers, qualifications, availability, leave, class mix, targets, custom rules/pins.

### Task 3: Add Bulk Matrix Operators

**Files:**
- Modify: `web/template.html`

- [ ] Rebuild Daily Targets as an editable matrix with row, column, and cell selection.
- [ ] Add bulk operations: set target, increment target, set max, max equals target plus N, copy row, copy column, weekday/weekend templates, clear selected overrides.
- [ ] Reuse the same matrix pattern for Class Mix min/max by location and class format.

### Task 4: Make Settings Canonical During Save

**Files:**
- Modify: `web/template.html`

- [ ] Add a normalization step before saving schedule config: ensure `targets`, `class_mix`, `manual_protected`, `manual_excluded`, `custom_rules`, `inactive_trainers`, and `source_of_truth` exist.
- [ ] Add validation: target cannot exceed max, negative values are blocked, duplicate manual pins are flagged, and hard conflicts block save.
- [ ] Add conflict display: settings override planner defaults and soft rules; hard-invalid configs require explicit correction before save.

### Task 5: Verify and Regenerate

**Files:**
- Modify/generated: `web/index.html`, `web/schedule_data.json`, outputs as reporter side effects if regenerated.

- [ ] Run focused pytest.
- [ ] Run reporter to regenerate the web interface from `web/template.html`.
- [ ] Run a local server and verify the Settings tab loads, matrices render, bulk operators mutate values, validation blocks bad values, and save calls succeed.
