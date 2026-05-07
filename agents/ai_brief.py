"""
Agent 4.5 — AI Scheduling Brief
Calls an OpenRouter/OpenAI-compatible API to produce context-aware scheduling hints before the optimiser runs.
Writes structured JSON that Agent 5 reads to bias slot selection and trainer assignment.
Gracefully skips if OPENROUTER_API_KEY is absent or the openai package is not installed.
"""
import json
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional

from ai_provider import OPENAI_AVAILABLE, create_ai_client, create_chat_completion

STATE_DIR = Path("state")

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

SYSTEM_PROMPT = """You are an expert studio schedule analyst for Physique 57 India. You specialise in data-driven scheduling that maximises fill rate, revenue, and trainer utilisation.

Default policy:
- Only universal scheduling guardrails are defined by default.
- Do not assume trainer-, studio-, or class-specific rules unless they are present in saved Settings/custom rules or supplied in the prompt.
- Use performance data, trainer availability, and saved user rules to produce scheduling hints.

Your task: Analyse performance data and return scheduling hints as a JSON object. Be specific about which class+trainer+day+time combos to prioritise or avoid. Consider weekly momentum, trainer fatigue risk, underperforming time bands, and class format diversity without inventing unsaved rules."""


@dataclass
class PriorityHint:
    location: str
    class_name: str
    trainer: str
    day: int          # 0=Monday...6=Sunday
    time: str         # "HH:MM"
    boost: float      # score points to add (5-25 typical)
    reason: str


@dataclass
class AvoidHint:
    location: str
    class_name: str
    trainer: str
    day: int
    penalty: float    # score points to subtract (5-30 typical)
    reason: str


@dataclass
class AIBrief:
    target_week: str
    priority_hints: List[dict] = field(default_factory=list)
    avoid_hints: List[dict] = field(default_factory=list)
    daily_target_overrides: Dict[str, Dict[str, int]] = field(default_factory=dict)
    class_mix_boosts: Dict[str, Dict[str, float]] = field(default_factory=dict)
    strategic_notes: List[str] = field(default_factory=list)
    model_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


class AISchedulingBrief:
    """
    Agent 4.5: Pre-optimiser AI brief.
    Produces structured hints that bias the optimiser's slot selection.
    """

    MAX_TOKENS = 3000

    def run(self) -> dict:
        print("[Agent 4.5] AI Brief starting...")

        if not OPENAI_AVAILABLE:
            print("[Agent 4.5] openai package not installed — skipping")
            print("[Agent 4.5]   pip install openai")
            return {"status": "skipped", "reason": "no_package"}

        client, settings = create_ai_client()
        if not client or not settings:
            print("[Agent 4.5] No OPENROUTER_API_KEY — skipping AI brief")
            return {"status": "skipped", "reason": "no_api_key"}

        try:
            scores = self._load_json(STATE_DIR / "03_scores.json")
            metrics = self._load_json(STATE_DIR / "02_metrics.json")
        except FileNotFoundError as e:
            print(f"[Agent 4.5] Missing state file: {e} — run agents 1-4 first")
            return {"status": "skipped", "reason": "missing_state"}

        # Load optional previous violation data
        prev_violations = []
        prev_sched_file = STATE_DIR / "05_draft_schedule_v1.json"
        if prev_sched_file.exists():
            prev = self._load_json(prev_sched_file)
            prev_violations = [
                s for s in prev.get("schedule", [])
                if s.get("constraint_violations")
            ][:20]

        prompt_data = self._build_prompt_data(scores, metrics, prev_violations)
        prompt = self._render_prompt(prompt_data)

        try:
            response = create_chat_completion(
                client=client,
                model=settings["model"],
                system_prompt=SYSTEM_PROMPT,
                user_prompt=prompt,
                max_tokens=self.MAX_TOKENS,
            )
        except Exception as e:
            print(f"[Agent 4.5] AI API call failed: {e}")
            return {"status": "error", "reason": str(e)}

        raw = (response.choices[0].message.content or "").strip()
        brief = self._parse_response(raw, prompt_data["target_week"])
        brief.model_used = settings["model"]
        brief.input_tokens = getattr(response.usage, "prompt_tokens", 0)
        brief.output_tokens = getattr(response.usage, "completion_tokens", 0)
        brief.cache_read_tokens = 0

        output = asdict(brief)
        STATE_DIR.mkdir(exist_ok=True)
        with open(STATE_DIR / "04b_ai_brief.json", "w") as f:
            json.dump(output, f, indent=2)

        print(f"[Agent 4.5] AI Brief complete — "
              f"{len(brief.priority_hints)} boosts, "
              f"{len(brief.avoid_hints)} penalties, "
              f"tokens: {brief.input_tokens}in / {brief.output_tokens}out "
              f"(cache_read={brief.cache_read_tokens})")
        return output

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt_data(self, scores: dict, metrics: dict, prev_violations: list) -> dict:
        ranking = scores["class_slot_ranking"]
        trainer_metrics = metrics["trainer_metrics"]
        day_band = metrics.get("day_band_metrics", [])

        # Top 20 performers per location (fill ≥ 0.35, sessions ≥ 10)
        top_by_loc = defaultdict(list)
        for r in ranking:
            if r.get("avg_fill_rate", 0) >= 0.35 and r.get("session_count", 0) >= 10:
                top_by_loc[r["location"]].append(r)
        for loc in top_by_loc:
            top_by_loc[loc] = sorted(top_by_loc[loc], key=lambda x: -x["score"])[:20]

        # Underperformers per location (fill < 0.25, sessions ≥ 8)
        under_by_loc = defaultdict(list)
        for r in ranking:
            if r.get("avg_fill_rate", 0) < 0.25 and r.get("session_count", 0) >= 8:
                under_by_loc[r["location"]].append(r)
        for loc in under_by_loc:
            under_by_loc[loc] = sorted(under_by_loc[loc], key=lambda x: x["avg_fill_rate"])[:15]

        # Trainer summary per location
        trainer_by_loc = defaultdict(list)
        for t in trainer_metrics:
            trainer_by_loc[t["location"]].append(t)
        for loc in trainer_by_loc:
            trainer_by_loc[loc].sort(key=lambda x: -x["trainer_avg_checkin"])

        # Day-band weaknesses (avg_fill_rate < 0.30, sessions ≥ 20)
        weak_bands = [
            d for d in day_band
            if d.get("avg_fill_rate", 1) < 0.30 and d.get("session_count", 0) >= 20
        ]

        # Determine week from scores or use placeholder
        target_week = "2026-05-04"

        return {
            "target_week": target_week,
            "top_by_loc": {k: v for k, v in top_by_loc.items()},
            "under_by_loc": {k: v for k, v in under_by_loc.items()},
            "trainer_by_loc": {k: v for k, v in trainer_by_loc.items()},
            "weak_bands": weak_bands,
            "prev_violations": prev_violations,
        }

    def _render_prompt(self, data: dict) -> str:
        loc_abbrev = {
            "Kwality House, Kemps Corner": "Kwality",
            "Supreme HQ, Bandra": "Supreme",
            "Kenkere House": "Kenkere",
        }

        sections = [f"## Schedule Week: {data['target_week']}\n"]

        for loc, abbr in loc_abbrev.items():
            tops = data["top_by_loc"].get(loc, [])
            unders = data["under_by_loc"].get(loc, [])
            trainers = data["trainer_by_loc"].get(loc, [])

            sections.append(f"### {abbr}")

            if tops:
                sections.append("**Top performers** (class | trainer | day | time | fill% | score):")
                for r in tops[:15]:
                    day = DAY_NAMES[r["day"]] if 0 <= r["day"] <= 6 else r["day"]
                    sections.append(
                        f"  {r['class']} | {r['trainer']} | {day} {r['time']} | "
                        f"fill={r['avg_fill_rate']:.0%} sessions={r['session_count']} score={r['score']:.0f}"
                    )

            if unders:
                sections.append("**Underperformers** (fill < 25%):")
                for r in unders[:10]:
                    day = DAY_NAMES[r["day"]] if 0 <= r["day"] <= 6 else r["day"]
                    sections.append(
                        f"  {r['class']} | {r['trainer']} | {day} {r['time']} | "
                        f"fill={r['avg_fill_rate']:.0%} sessions={r['session_count']}"
                    )

            if trainers:
                sections.append("**Trainer rankings** (avg check-in | fill | sessions):")
                for t in trainers[:10]:
                    sections.append(
                        f"  {t['trainer']}: checkin={t['trainer_avg_checkin']:.1f} "
                        f"fill={t['trainer_fill_rate']:.0%} sessions={t['trainer_session_count']}"
                    )
            sections.append("")

        if data["weak_bands"]:
            sections.append("### Weak day-band combinations (fill < 30%, sessions ≥ 20):")
            for d in data["weak_bands"][:12]:
                day = DAY_NAMES[d["day"]] if 0 <= d["day"] <= 6 else d["day"]
                sections.append(
                    f"  {d['location'].split(',')[0]} | {day} {d['band']} | "
                    f"fill={d['avg_fill_rate']:.0%} sessions={d['session_count']}"
                )
            sections.append("")

        if data["prev_violations"]:
            sections.append("### Constraint violations from last schedule run:")
            for v in data["prev_violations"][:10]:
                day = v.get("day_of_week", "")
                viols = "; ".join(v.get("constraint_violations", []))
                sections.append(
                    f"  {v['location'].split(',')[0]} | {day} {v['time']} | "
                    f"{v['class_name']} / {v['trainer_1']} | {viols}"
                )
            sections.append("")

        sections.append("""## Your task

Analyse the above data and return a JSON object with these exact keys:

```json
{
  "priority_hints": [
    {
      "location": "<full location name>",
      "class_name": "<exact class name>",
      "trainer": "<exact trainer name>",
      "day": <0-6>,
      "time": "<HH:MM>",
      "boost": <5.0-25.0>,
      "reason": "<one line>"
    }
  ],
  "avoid_hints": [
    {
      "location": "<full location name>",
      "class_name": "<exact class name>",
      "trainer": "<exact trainer name>",
      "day": <0-6>,
      "penalty": <5.0-30.0>,
      "reason": "<one line>"
    }
  ],
  "daily_target_overrides": {
    "<location>": {
      "<day>": <integer class count>
    }
  },
  "class_mix_boosts": {
    "<location>": {
      "<class_name>": <score_boost_float>
    }
  },
  "strategic_notes": ["<note 1>", "<note 2>", "<note 3>"]
}
```

Rules for hints:
- priority_hints: combos to schedule ahead of their score rank. Boost 15-25 for clear winners, 5-14 for moderate preferences.
- avoid_hints: combos to deprioritize. Penalty 20-30 for strong avoids, 5-19 for mild.
- daily_target_overrides: only set if you have a specific reason to change from historical averages. Omit keys you want left at default.
- class_mix_boosts: score bonus for an entire class format at a location (e.g., push PowerCycle higher at Supreme).
- strategic_notes: 3 specific, actionable insights.

Return ONLY the JSON object, no markdown, no explanation.""")

        return "\n".join(sections)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, raw: str, target_week: str) -> AIBrief:
        # Strip markdown code fences if present
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            print("[Agent 4.5] JSON parse failed — returning empty brief")
            return AIBrief(target_week=target_week)

        brief = AIBrief(target_week=target_week)

        # Validate and normalise priority hints
        for h in data.get("priority_hints", []):
            if all(k in h for k in ("location", "class_name", "trainer", "day", "time", "boost")):
                h["boost"] = min(30.0, max(0.0, float(h["boost"])))
                brief.priority_hints.append(h)

        # Validate and normalise avoid hints
        for h in data.get("avoid_hints", []):
            if all(k in h for k in ("location", "class_name", "trainer", "day", "penalty")):
                h["penalty"] = min(35.0, max(0.0, float(h["penalty"])))
                brief.avoid_hints.append(h)

        brief.daily_target_overrides = data.get("daily_target_overrides", {})
        brief.class_mix_boosts = data.get("class_mix_boosts", {})
        brief.strategic_notes = data.get("strategic_notes", [])[:5]

        return brief

    @staticmethod
    def _load_json(path: Path) -> dict:
        with open(path) as f:
            return json.load(f)
