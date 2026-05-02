# Physique 57 India — Schedule Intelligence

## Overview
A 6-agent AI pipeline that ingests historical class data, scores every trainer×class×slot combination, applies configurable rules, then uses GPT-4o to draft an optimised weekly schedule for three studio locations.

**Locations:** Kwality House (Kemps Corner), Supreme HQ (Bandra), Kenkere House

---

## Architecture

### Agent Pipeline (run by `orchestrator.py`)
| Step | Agent | Input | Output |
|------|-------|-------|--------|
| 1 | **Ingestor** (`agents/ingestor.py`) | `Sessions Performance Data.csv` | `state/01_sessions.json` |
| 2 | **Analyst** (`agents/analyst.py`) | `01_sessions.json` | `state/02_metrics.json` |
| 3 | **Scorer** (`agents/scorer.py`) | `Class Performance by Trainer.csv` + `01_sessions.json` | `state/03_scores.json` |
| 4 | **Rule Engine** (`agents/rule_engine.py`) | `03_scores.json` + rules | `state/04_constraints.json` |
| 5 | **AI Planner** (`agents/ai_planner.py`) | `03_scores.json` + `02_metrics.json` + rules | `state/05_schedule.json` |
| 6 | **Reporter** (`agents/reporter.py`) | `05_schedule.json` | `outputs/` (HTML, CSV, JSON) |

### Web Server
- `serve.py` — Python HTTP server on port **5000**
- Serves `web/index.html` (schedule dashboard)
- API endpoints: `/api/rules-config`, `/api/save-rules`, `/api/run-pipeline`, `/api/pipeline-status`

### Rules System
- Rules stored in `rules/universal_rules.json`, `rules/location_*.json`, etc.
- Rule categories managed in `rule_config.py`
- Active config stored in `config/rules_config.json`
- `config/trainer_overrides.json` — trainer-specific availability and owned blocks

---

## Key Files

```
orchestrator.py          — Pipeline entry point
serve.py                 — Web server (port 5000)
rule_config.py           — Rule category definitions and config management
agents/
  ingestor.py            — CSV ingestion and normalisation
  analyst.py             — Recency-weighted historical metrics (8-week window)
  scorer.py              — Composite slot scoring with recency boost
  rule_engine.py         — Hard/soft constraint compilation
  ai_planner.py          — GPT-4o prompt builder and schedule enforcer
  reporter.py            — Schedule output formatting
rules/
  universal_rules.json   — Cross-location constraints
  location_kwality.json  — Kwality House rules
  location_supreme.json  — Supreme HQ rules
  location_kenkere.json  — Kenkere House rules
config/
  rules_config.json      — Active category enable/disable state
  trainer_overrides.json — Trainer availability and owned blocks
state/                   — Pipeline intermediate outputs (JSON)
outputs/                 — Final schedule files (HTML, CSV, XLSX, JSON)
web/
  index.html             — Single-file schedule dashboard (React-free, vanilla JS)
```

---

## Running the System

### Start Web Server
```bash
python3 serve.py --port 5000 --week 2026-05-04 --csv "Sessions Performance Data.csv"
```

### Run Full Pipeline (command line)
```bash
python3 orchestrator.py --csv "Sessions Performance Data.csv" --week 2026-05-04
```

### Run from UI
Click the **Generate** button in the top-right of the web dashboard.

---

## Scoring Weights
- `blended_fill`: 40% — fill rate (slot-specific Bayesian-blended with trainer average)
- `blended_checkin`: 30% — average check-ins per class
- `longevity`: 20% — session history depth
- `rev_per_session`: 10% — revenue per session
- `recency_boost`: ±8 points — momentum adjustment from last 8 weeks vs all-time fill

Slot trust ramps from 0→1 between 5 and 20 sessions (replaces old linear 0–10 ramp).

---

## Analyst Recency Weighting
Sessions in the last **8 weeks** receive a **3× weight** in fill-rate and check-in averages.
Additional fields added per metric row:
- `avg_fill_rate_recency` — 8-week weighted fill rate
- `avg_checkin_recency` — 8-week weighted check-in
- `recency_momentum` — ratio of 8-week fill to all-time fill (1.0 = flat, >1 = growing)
- `sessions_last_8_weeks` — count of sessions in last 8 weeks

---

## Rule Categories (all enabled by default except `trainer_specific`)
| Category | Description |
|----------|-------------|
| `universal` | Barre mix targets, Sunday rules, consecutive-class limits |
| `format_rules` | Per-format slot counts, eligible locations, mix targets |
| `location_kwality` | Kwality-specific rules (Strength Lab, PowerCycle) |
| `location_supreme` | Supreme-specific rules (daily PowerCycle, Recovery) |
| `location_kenkere` | Kenkere-specific rules (PowerCycle ban, Barre mins) |
| `trainer_specific` | Treats trainer availability as hard locks (OFF = soft preference) |

---

## Web Dashboard Features
- **Grid / Timeline / List / Trainer / Cross-Location / Analytics / Rules** views
- Click any class card to see full detail modal (fill, check-in, history, violations, reason)
- **Rules tab** — full-page rules editor: toggle categories, enable/disable individual rules, edit rule text, save changes
- **Generate button** — triggers pipeline re-run with live progress bar and stage labels
- **Pipeline status bar** — polls `/api/pipeline-status` every 8s while running
- **Toast notifications** — success/warning/error feedback for all actions
- Filter bar collapses automatically when switching to Rules view
