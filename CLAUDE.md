# Studio Schedule Optimisation — Agent Team

## Project Overview

Build a multi-agent Python system that ingests class performance CSV data, scores and ranks every class format × trainer × time-slot combination per studio location, enforces a strict rulebook of scheduling constraints, and outputs a fully optimised weekly schedule for each location — formatted to match the existing schedule template CSV.

The system covers **3 studio locations**:
- **Kwality House, Kemps Corner** (Mumbai) — flagship, broadest offering
- **Supreme HQ, Bandra** (Mumbai) — PowerCycle-equal hub
- **Kenkere House** (Bengaluru) — Barre-pure community studio

Source data: `Sessions_Performance_Data.csv` (~20,786 session records).
Schedule template: `Schedule_Views_-_Schedule__6_.csv` (the column structure to match on output).

---

## Architecture — 6 Agents + Orchestrator

Build each agent as a Python class with a single `run()` method. Agents are called sequentially by the Orchestrator. All inter-agent data is passed as Python dicts/dataclasses and written to a shared `state/` directory as JSON for debugging and resumability.

```
orchestrator.py          # Entry point. Calls agents in order. Handles retries.
agents/
  ingestor.py            # Agent 1 — data normalisation
  analyst.py             # Agent 2 — metrics per class × location
  scorer.py              # Agent 3 — composite score + ranking
  rule_engine.py         # Agent 4 — constraint validation
  optimiser.py           # Agent 5 — schedule slot assignment
  reporter.py            # Agent 6 — output formatting
rules/
  universal_rules.json   # Hard constraints applying to all locations
  kwality_rules.json     # Kwality House specific
  supreme_rules.json     # Supreme HQ specific
  kenkere_rules.json     # Kenkere House specific
  trainer_profiles.json  # All 30 trainers: tiers, availability, qualifications
  class_formats.json     # All 19 class formats: duration, eligibility, slot rules
state/                   # Written by each agent, read by next
  01_sessions.json
  02_metrics.json
  03_scores.json
  04_constraints.json
  05_draft_schedule.json
outputs/
  schedule_kwality.csv
  schedule_supreme.csv
  schedule_kenkere.csv
  scorecard.json
```

---

## Agent 1 — Data Ingestor (`agents/ingestor.py`)

**Input:** `Sessions_Performance_Data.csv`

**Responsibilities:**
1. Load the CSV with pandas. Column map:
   - `TrainerID`, `Trainer`, `SessionID`, `SessionName`, `Capacity`, `CheckedIn`, `LateCancelled`, `Booked`, `Complimentary`, `Location`, `Date`, `Day`, `Time`, `Revenue`, `NonPaid`, `Type`, `Class`
2. Filter to only the 3 permanent studio locations:
   - `"Kwality House, Kemps Corner"`, `"Supreme HQ, Bandra"`, `"Kenkere House"`
   - Drop: `Pop-up`, `WeWork Galaxy`, `WeWork Prestige Central`, `South United Football Club`, `The Studio by Copper + Cloves`
3. Parse `Date` as datetime. Parse `Time` as `HH:MM` string.
4. Add derived columns:
   - `fill_rate` = `CheckedIn / Capacity` (cap at 1.0)
   - `no_show_rate` = `(Booked - CheckedIn) / Booked` where Booked > 0 else 0
   - `late_cancel_rate` = `LateCancelled / Booked` where Booked > 0 else 0
   - `revenue_per_seat` = `Revenue / CheckedIn` where CheckedIn > 0 else 0
   - `day_of_week` = from Date (Monday=0 … Sunday=6)
   - `time_band` = `morning` (07:00–09:59) / `midday` (10:00–12:59) / `afternoon` (13:00–16:59) / `evening` (17:00–20:30)
5. Write cleaned dataframe to `state/01_sessions.json`.

**Output schema:**
```json
{
  "locations": ["Kwality House, Kemps Corner", "Supreme HQ, Bandra", "Kenkere House"],
  "total_sessions": 18500,
  "date_range": {"min": "2024-01-01", "max": "2026-05-31"},
  "sessions": [ { ...one row per cleaned record... } ]
}
```

---

## Agent 2 — Performance Analyst (`agents/analyst.py`)

**Input:** `state/01_sessions.json`

**Responsibilities:**

For every unique combination of `(location, Class, Trainer, day_of_week, Time)` calculate:
- `session_count` — number of times this combo appeared
- `avg_checkin` — mean CheckedIn
- `avg_fill_rate` — mean fill_rate
- `avg_revenue` — mean Revenue
- `avg_revenue_per_seat` — mean revenue_per_seat
- `avg_late_cancel_rate`
- `avg_no_show_rate`
- `total_revenue` — sum Revenue
- `trend_fill_rate` — linear slope of fill_rate over time (positive = improving)
- `sessions_last_12_weeks` — count in most recent 12 calendar weeks

Also compute location-level aggregates:
- By `(location, Class)`: aggregate metrics across all trainers and times
- By `(location, Trainer)`: `trainer_avg_checkin`, `trainer_fill_rate`, `trainer_session_count`
- By `(location, Time)`: slot-level fill rate and volume

Write to `state/02_metrics.json`.

**Output schema:**
```json
{
  "class_trainer_slot_metrics": [ { "location": "...", "class": "...", "trainer": "...", "day": 0, "time": "09:00", "session_count": 42, "avg_fill_rate": 0.48, ... } ],
  "class_metrics": [ { "location": "...", "class": "...", "avg_fill_rate": 0.42, ... } ],
  "trainer_metrics": [ { "location": "...", "trainer": "...", "avg_checkin": 5.6, ... } ],
  "slot_metrics": [ { "location": "...", "time": "09:00", "avg_fill_rate": 0.46, ... } ]
}
```

---

## Agent 3 — Class Scorer (`agents/scorer.py`)

**Input:** `state/02_metrics.json`

**Responsibilities:**

Compute a **composite score (0–100)** for every `(location, Class, Trainer, day_of_week, Time)` combination. Use this weighting formula:

```
score = (
  avg_fill_rate       * 35 +
  avg_revenue_norm    * 25 +   # normalised 0–1 within location
  avg_checkin_norm    * 20 +   # normalised 0–1 within location
  session_count_norm  * 10 +   # recency and frequency signal
  trend_fill_rate_norm * 10    # positive trend gets bonus
)
```

Normalise each sub-score 0–1 within its location before applying weights.

Then produce **ranked lists** at three levels:
1. `class_slot_ranking` — top slots per class per location (used by Optimiser)
2. `trainer_ranking` — trainers ranked by avg_checkin within each location
3. `class_type_ranking` — which class formats perform best at each location

Assign each entry a `recommendation`:
- `PROTECT` — score ≥ 70: must appear in the schedule
- `INCLUDE` — score 45–69: include if slot is available
- `CONSIDER` — score 25–44: optional, fill remaining slots
- `DROP` — score < 25: do not schedule unless forced by class mix rules

Write to `state/03_scores.json`.

---

## Agent 4 — Rule Engine (`agents/rule_engine.py`)

**Input:** `state/03_scores.json` + rule JSON files

**Responsibilities:**

Load all rules from `rules/` and produce a **constraint set** that the Optimiser must respect. Encode rules as structured objects:

```python
@dataclass
class HardConstraint:
    constraint_id: str
    type: str          # "never_do" | "always_do" | "trainer_availability" | "class_location" | "slot_required"
    location: str      # None = all locations
    description: str
    check_fn_name: str # name of a validator function to call

@dataclass
class SoftConstraint:
    constraint_id: str
    priority: int      # 1 (highest) to 5 (lowest)
    description: str
    penalty: float     # subtracted from score if violated
```

**Encode every rule below as a HardConstraint or SoftConstraint.**

### Universal Hard Constraints (all locations)

```
UNIV-001: Barre 57 family must be ≥ 25% of total weekly classes at any location
UNIV-002: All 3 time bands must have ≥ 1 class every weekday (morning / midday / evening)
UNIV-003: Saturday must be max-load day — never fewer classes than any other day of the week
UNIV-004: Sunday max 5–6 classes. No class before 10:00. No evening band.
UNIV-005: Specialist classes (PowerCycle, Strength Lab, Pre/Post Natal, Foundations) — only certified trainers
UNIV-006: Any Express class must be paired with a full-length equivalent of the same type on the same day
UNIV-007: Studio Recovery must NEVER be the first class of the day
UNIV-008: Foundations must never be scheduled in 11:30 or 19:15 slots
UNIV-009: No trainer > 3 consecutive classes without ≥ 30 min gap
UNIV-010: No trainer > 4 classes in one day
UNIV-011: PowerCycle NEVER at Kenkere House
UNIV-012: Strength Lab ONLY at Kwality House
UNIV-013: Pre/Post Natal ONLY at Kwality House
UNIV-014: Peak midday slot must never be empty at any location
UNIV-015: Always schedule ≥ 1 Barre 57 before 10:00 AM every day
UNIV-016: Always schedule Barre 57 or Cardio Barre in the 19:00–19:30 window on weekdays
UNIV-017: Always assign a Tier 1 trainer to the primary peak slot
UNIV-018: Trainer substitution protocol: same tier + same class → one tier lower + same class → cross-location trainer with ≥ 30 sessions at that location
UNIV-019: Specialist classes (PowerCycle, Strength Lab, Pre/Post Natal) — NO substitution with non-certified trainers
UNIV-020: Never roster a trainer at a location where they have < 30 historical sessions
```

### Kwality House Hard Constraints

```
KW-001: 11:30 AM slot must always be filled with a Tier 1 trainer
KW-002: Anisha Shah → Mon/Tue/Wed ONLY. Never Thu/Sat/Sun at Kwality.
KW-003: Mrigakshi Jaiswal → Thu/Fri ONLY at Kwality.
KW-004: Pranjali Jain owns Saturday 10:15–12:30 block
KW-005: Rohan Dahima owns Thursday morning (09:15, 10:15) block
KW-006: Strength Lab → Atulan Purohit EXCLUSIVELY. Mon/Wed evenings only. Max 2×/week.
KW-007: Pre/Post Natal → Anisha Shah (Mon–Wed), Mrigakshi Jaiswal (Thu–Fri). Mornings only (08:30–11:30).
KW-008: Strength Lab max 2× per week total
```

### Supreme HQ Hard Constraints

```
SU-001: PowerCycle must be treated as equal pillar to Barre 57 — minimum 2 PowerCycle classes per day on weekdays
SU-002: Cauveri Vikrant is first-choice for ALL PowerCycle slots at Supreme
SU-003: Anisha Shah → THURSDAYS ONLY at Supreme HQ. Never any other day. Teaches 08:00–11:00 block only.
SU-004: Vivaran Dhasmana owns Tue/Wed morning blocks (07:30–11:00) at Supreme
SU-005: Karan Bhatia owns Sunday 10:15 and 11:30 blocks at Supreme
SU-006: Atulan Purohit owns Fri/Sat morning block (09:00–11:30) at Supreme
SU-007: Mrigakshi Jaiswal → Mon/Tue/Wed evenings only at Supreme. Not available other days here.
SU-008: 18:00 PM slot must always have a Tier 1 trainer on weekdays
SU-009: No Strength Lab at Supreme HQ
SU-010: No Pre/Post Natal at Supreme HQ
```

### Kenkere House Hard Constraints

```
KE-001: PowerCycle NEVER — not for any reason
KE-002: Kajol Kanchan is 6-day anchor — never reduce her allocation without confirmed coverage
KE-003: Pushyank Nahar owns Mon/Tue/Thu morning blocks (07:15, 09:00, 11:00)
KE-004: Shruti Kulkarni owns Thu/Fri mornings and Sunday midday
KE-005: Veena Narasimhan → weekends ONLY, 16:00–17:15 window ONLY
KE-006: Chaitanya Nahar → Mon/Sat/Sun afternoons only (16:00–18:45). Never mornings.
KE-007: 09:00 AM must be filled with a Tier 1 trainer every day
KE-008: Saturday 09:00–11:00 block → Kajol Kanchan or Pushyank Nahar ONLY
KE-009: Foundations certified trainers only: Shruti Kulkarni, Poojitha Bhaskar, Siddhartha Kusuma, Shruti Suresh, Pushyank Nahar, Kajol Kanchan
```

### Class Mix Soft Constraints (penalty if violated)

```
MIX-001 (priority 1): Barre 57 family 45–55% of weekly classes (penalty 10 per % below 45)
MIX-002 (priority 1): PowerCycle 8–10% at Kwality, 25–28% at Supreme, 0% at Kenkere
MIX-003 (priority 2): Mat 57 minimum 3–4× per week at each location
MIX-004 (priority 2): FIT 1× daily, morning slots
MIX-005 (priority 2): Foundations at Kenkere 1× daily; Kwality/Supreme 2–3×/week
MIX-006 (priority 3): Recovery weekends only, afternoon slots (12:30–16:00)
MIX-007 (priority 3): Back Body Blaze morning only (07:30–09:00), max 3×/week
MIX-008 (priority 3): Studio Amped Up! max 1–2×/week; Reshma or Rohan only
MIX-009 (priority 4): Cardio Barre 1×/day, evenings preferred
MIX-010 (priority 4): Strength Lab max 2×/week at Kwality, Mon/Wed evenings only
```

### Slot Priority Soft Constraints

```
SLOT-001 (priority 1): Always fill peak midday (11:00–11:30) and prime evening (19:00–19:30)
SLOT-002 (priority 2): Fill morning block (08:00–09:30) and early evening (17:45–18:15)
SLOT-003 (priority 3): Fill early morning (07:00–07:30) and late evening (20:00) if trainers available
SLOT-004 (priority 4): Mid-afternoon (13:00–16:00) optional — location dependent
```

Write to `state/04_constraints.json` — a serialised form of all HardConstraint and SoftConstraint objects.

---

## Agent 5 — Schedule Optimiser (`agents/optimiser.py`)

**Input:** `state/03_scores.json` + `state/04_constraints.json`

**Responsibilities:**

Generate a complete 7-day schedule for each location. The schedule is a list of `ScheduleSlot` objects:

```python
@dataclass
class ScheduleSlot:
    location: str
    date: str          # ISO date of the week being scheduled
    day_of_week: str   # "Monday" ... "Sunday"
    time: str          # "09:00"
    class_name: str
    trainer_1: str
    trainer_2: str     # optional co-trainer
    cover: str         # substitution trainer if primary unavailable
    capacity: int
    predicted_fill_rate: float
    score: float
    constraint_violations: list[str]   # empty list if clean
```

**Algorithm — greedy slot-fill with constraint backtracking:**

```
FOR each location:
  FOR each day in [Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday]:
    
    1. Determine target_class_count for this day from location rules:
       - Kwality: Mon/Thu=10–11, Tue/Wed=9–10, Sat=9–10, Fri=8–9, Sun=5–6
       - Supreme: Thu/Sat/Mon=10–11, Tue/Fri=9–10, Wed=8–9, Sun=5–6
       - Kenkere: Sat=7–8, Mon–Thu=7, Fri=6, Sun=4–5

    2. Build candidate_slots = all (time, class, trainer) combos sorted by score DESC,
       filtered to day_of_week availability.

    3. Enforce pinned blocks first (owned slots from hard constraints):
       - Add these to the schedule unconditionally
       - Remove pinned trainers from available pool for those time windows

    4. GREEDY FILL LOOP:
       FOR each candidate (time, class, trainer) in score order:
         IF slot_count < target_class_count:
           check_hard_constraints(candidate, current_schedule)
           IF no violations:
             add to schedule
             update trainer_load_tracker
           ELSE:
             try next candidate for same time slot
         ELSE:
           break

    5. POST-FILL VALIDATION:
       - Check all universal hard constraints against the full day schedule
       - Check class mix percentages for the week so far
       - Flag any remaining violations in constraint_violations field
       - Log soft constraint penalties

    6. If any UNIV or location hard constraint is still violated after greedy fill:
       BACKTRACK: remove the most recent non-pinned slot and retry with next candidate
       Max 3 backtrack attempts per slot before flagging as UNRESOLVED

FOR the complete week schedule:
  - Check weekly class mix percentages
  - Flag if any class mix soft constraint is violated
  - Compute predicted_weekly_fill_rate as weighted avg of slot fill rates
```

Write to `state/05_draft_schedule.json`.

---

## Agent 6 — Output Reporter (`agents/reporter.py`)

**Input:** `state/05_draft_schedule.json` + `state/02_metrics.json`

**Responsibilities:**

1. **Format schedule CSVs** to match `Schedule_Views_-_Schedule__6_.csv` structure:
   - Columns: `Time, Location, Class, Trainer 1, Trainer 2, Cover, Theme` repeated for each day of the week
   - One row per time slot. Output one file per location.
   - Write to `outputs/schedule_kwality.csv`, `outputs/schedule_supreme.csv`, `outputs/schedule_kenkere.csv`

2. **Generate scorecard** (`outputs/scorecard.json`):
   ```json
   {
     "generated_for_week": "2026-05-04",
     "locations": {
       "Kwality House, Kemps Corner": {
         "total_classes": 68,
         "predicted_avg_fill_rate": 0.42,
         "historical_avg_fill_rate": 0.38,
         "class_mix": { "Barre 57": 0.26, "Cardio Barre": 0.10, ... },
         "hard_constraint_violations": [],
         "soft_constraint_penalties": 0,
         "optimisation_opportunities": [...]
       }
     }
   }
   ```

3. **Print a human-readable summary** to stdout after completion:
   - One section per location
   - Daily class count + predicted fill rate
   - Any unresolved constraint violations (RED flags)
   - Top 3 optimisation opportunities from the rulebook

---

## Trainer Profiles — Encode in `rules/trainer_profiles.json`

Encode ALL 30 trainers with this schema per trainer:

```json
{
  "name": "Anisha Shah",
  "tier": 1,
  "locations": {
    "Kwality House, Kemps Corner": {
      "available_days": ["Monday", "Tuesday", "Wednesday"],
      "time_window": {"start": "07:15", "end": "19:15"},
      "avg_classes_per_day": 1.9,
      "max_classes_per_day": 3,
      "avg_checkin": 7.7,
      "session_count": 280,
      "owned_blocks": [],
      "notes": "Highest avg attendance. Mon–Wed ONLY. Pre/Post Natal specialist."
    },
    "Supreme HQ, Bandra": {
      "available_days": ["Thursday"],
      "time_window": {"start": "07:15", "end": "12:00"},
      "avg_classes_per_day": 2.7,
      "max_classes_per_day": 4,
      "avg_checkin": 6.1,
      "session_count": 85,
      "owned_blocks": [{"day": "Thursday", "times": ["08:00", "09:00", "10:00", "11:00"]}],
      "notes": "THURSDAYS ONLY at Bandra. Never any other day."
    }
  },
  "qualifications": {
    "all_barre": true,
    "mat_57": true,
    "powercycle": true,
    "strength_lab": true,
    "foundations": true,
    "pre_post_natal": true,
    "amped_up": false,
    "hiit": false,
    "recovery": false
  }
}
```

Encode all 30 trainers from the rulebook. Key profiles to get exactly right:

**Kwality Tier 1:** Rohan Dahima (Mon/Tue/Wed/Thu/Sun, owns Thu morning), Pranjali Jain (Mon/Tue/Wed/Sat, owns Sat 10:15–12:30), Reshma Sharma (Mon/Tue/Sat, Amped Up specialist), Richard D'Costa (Mon/Tue/Thu/Fri/Sat, high versatility), Atulan Purohit (Mon/Tue/Wed/Fri, SOLE Strength Lab trainer), Anisha Shah (Mon/Tue/Wed only, highest avg 7.7), Mrigakshi Jaiswal (Thu/Fri only at Kwality).

**Supreme Tier 1:** Vivaran Dhasmana (Mon/Tue/Wed/Sat, owns Tue/Wed mornings), Karanvir Bhatia (Mon/Tue/Thu/Fri/Sat), Cauveri Vikrant (Mon/Thu/Fri/Sun, PRIMARY PowerCycle), Karan Bhatia (Mon/Wed/Sat/Sun, owns Sun morning block), Atulan Purohit (Thu/Fri/Sat at Supreme), Mrigakshi Jaiswal (Mon/Tue/Wed evenings only at Supreme), Anisha Shah (Thu only at Supreme).

**Kenkere Tier 1:** Kajol Kanchan (6-day anchor, Sat 09:00 mandatory), Shruti Kulkarni (Thu/Fri mornings + Sun midday), Pushyank Nahar (Mon/Tue/Thu mornings owner, Sun off).

**PowerCycle specialists (Kwality+Supreme only):** Bret Saldanha, Anmol Sharma, Raunak Khemuka — all Tier 3, off-peak only, very low avg attendance.

**Kenkere-only Tier 2/3:** Siddhartha Kusuma, Shruti Suresh, Poojitha Bhaskar, Chaitanya Nahar (Mon/Sat/Sun afternoons only), Veena Narasimhan (weekends 16:00–17:15 only).

---

## Class Format Rules — Encode in `rules/class_formats.json`

```json
[
  {
    "name": "Studio Barre 57",
    "family": "barre_57",
    "duration_min": 57,
    "intensity": "medium_high",
    "eligible_locations": ["all"],
    "best_days": ["all"],
    "preferred_slots": ["07:30", "09:00", "11:00", "11:30", "19:00"],
    "min_per_week": 14,
    "max_per_week": null,
    "target_pct": 0.30,
    "rules": [
      "min_2_per_weekday",
      "min_1_per_sunday",
      "always_tier1_at_1130"
    ]
  },
  {
    "name": "Studio PowerCycle",
    "family": "powercycle",
    "duration_min": 45,
    "intensity": "high",
    "eligible_locations": ["Kwality House, Kemps Corner", "Supreme HQ, Bandra"],
    "never_at": ["Kenkere House"],
    "preferred_slots": ["17:30", "18:00", "19:15", "19:30"],
    "rules": [
      "evening_only",
      "never_kenkere",
      "cauveri_first_choice_at_supreme",
      "pair_express_with_full_same_day"
    ]
  },
  {
    "name": "Studio Strength Lab",
    "family": "strength_lab",
    "eligible_locations": ["Kwality House, Kemps Corner"],
    "never_at": ["Supreme HQ, Bandra", "Kenkere House"],
    "preferred_slots": ["18:00", "19:15"],
    "preferred_days": ["Monday", "Wednesday"],
    "max_per_week": 2,
    "certified_trainers_only": ["Atulan Purohit"],
    "rules": ["kwality_only", "mon_wed_evenings_only", "atulan_exclusively"]
  }
]
```

Encode all 19 class formats following the same pattern.

---

## Orchestrator (`orchestrator.py`)

```python
def run_pipeline(
    csv_path: str,
    schedule_template_path: str,
    target_week_start: str,   # ISO date of the Monday to schedule
    locations: list[str] = None,  # None = all 3
    debug: bool = False
):
    """
    Runs the full 6-agent pipeline. Agents are called in sequence.
    State is persisted to state/ after each agent.
    If an agent fails, the error is logged and the pipeline halts.
    """
```

CLI interface:
```bash
python orchestrator.py \
  --csv Sessions_Performance_Data.csv \
  --template Schedule_Views_-_Schedule__6_.csv \
  --week 2026-05-04 \
  --location "Kwality House, Kemps Corner"
```

Without `--location`, run all 3 locations.

Add a `--resume` flag that skips agents whose state files already exist (useful during development).

---

## Key Optimisation Opportunities to Surface in Output

The reporter must always flag these as priority recommendations in the scorecard:

1. **Kenkere 10:00 AM** — 56.5% fill rate, only 144 sessions. Recommend expanding to 5 days/week. Highest ROI change available.
2. **Supreme 09:30 AM** — 46.7% fill, only 349 sessions. Expand to daily with Cauveri or Atulan.
3. **Anisha Shah at Kwality on Fridays** — avg 7.7 check-in, currently Mon/Wed only. Even 1 Friday morning slot would lift Friday's weakest day.
4. **Kwality Thursday fill rate** — 33.5%, lowest despite high volume. Either cut 1–2 Thu slots or swap trainer/class mix.
5. **Kenkere Saturday 4th morning slot** — 40.6% fill, currently 7–8 classes. Kajol or Shruti Kulkarni as candidates.
6. **Supreme Friday underperformance** — 31.2% fill. Test Vivaran or Cauveri on Fridays instead of current assignment.

---

## Scoring Weights (configurable via CLI)

Allow overriding via `--scoring-weights` JSON string:
```json
{
  "fill_rate": 0.35,
  "revenue": 0.25,
  "avg_checkin": 0.20,
  "session_frequency": 0.10,
  "trend": 0.10
}
```

---

## Dependencies

```
pandas>=2.0
numpy>=1.24
python-dateutil>=2.8
rich>=13.0        # for CLI output formatting
click>=8.0        # for CLI argument parsing
```

No Claude API calls required — all agents run deterministically from data + rule files. The intelligence is encoded in the scoring formula and constraint system, not in LLM inference.

---

## Testing

Create `tests/test_constraints.py` with unit tests for every hard constraint validator function. Each test should:
1. Build a minimal schedule that violates exactly one constraint
2. Assert that `check_hard_constraints()` catches it
3. Build a valid schedule variant
4. Assert no violations

Create `tests/test_scorer.py` with tests that verify:
- Scores are always in [0, 100]
- A known high-performer (Anisha Shah, Kwality, 09:00, Mon) scores ≥ 70
- A known low-performer (Raunak Khemuka, Supreme, any peak slot) scores < 30

---

## Output Quality Checks

Before writing final output files, the reporter must run these assertions and halt with a clear error if any fail:

```python
assert schedule_kwality["total_classes"] >= 55  # min reasonable week
assert schedule_kwality["barre_pct"] >= 0.25
assert schedule_supreme["powercycle_pct"] >= 0.20
assert schedule_kenkere["powercycle_count"] == 0
assert all slot["trainer_1"] != "" for slot in final_schedule
assert no "Strength Lab" in schedule_supreme
assert no "Strength Lab" in schedule_kenkere
assert no class before "10:00" on any Sunday at any location
```

---

## File Naming Convention

Input files expected in project root. Do not hardcode paths — use `pathlib.Path` throughout. Output directory is `outputs/` relative to project root, created if absent.

---

## Notes for Implementation

- Use `dataclasses` throughout for type safety on all inter-agent data structures
- The constraint system should be table-driven (rule objects), not a pile of if/else blocks
- The optimiser backtracking depth is limited to 3 per slot to keep runtime reasonable on a 7-day, 3-location run
- All monetary values are in INR; no currency conversion needed
- The `Cover` column in the output schedule should be populated using the substitution hierarchy: same tier + same class type first, then one tier lower
- Print clear progress logs to stdout: `[Agent 1] Ingestor complete — 18,503 sessions across 3 locations`
- If a hard constraint cannot be satisfied after backtracking, write the violation to the schedule slot's `constraint_violations` list and continue — never silently drop a slot
