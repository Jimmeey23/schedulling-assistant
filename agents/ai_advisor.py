import json
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Any

from ai_provider import OPENAI_AVAILABLE, create_ai_client, create_chat_completion

STATE_DIR = Path("state")
OUTPUT_DIR = Path("outputs")


@dataclass
class SwapRecommendation:
    location: str
    day: str
    time: str
    current_class: str
    current_trainer: str
    suggested_class: str
    suggested_trainer: str
    reasoning: str
    confidence_pct: int


@dataclass
class TrainerWorkloadFlag:
    trainer: str
    location: str
    issue_type: str  # "overuse" | "underuse"
    details: str


@dataclass
class AIInsights:
    schedule_assessment: Dict[str, str]
    swap_recommendations: List[SwapRecommendation]
    strategic_insights: List[str]
    trainer_workload_flags: List[TrainerWorkloadFlag]
    class_mix_verdict: Dict[str, str]
    generated_at: str
    model_used: str
    cache_stats: Dict[str, int]


class AISchedulingAdvisor:
    """
    Agent 7: AI-powered schedule advisor using an OpenRouter/OpenAI-compatible API.
    Analyzes the generated schedule and provides strategic insights.
    Gracefully skips if no API key or the openai package is unavailable.
    """
    MAX_TOKENS = 2000

    def run(self) -> dict:
        print("[Agent 7] AI Advisor starting...")

        if not OPENAI_AVAILABLE:
            print("[Agent 7] openai package not installed — skipping AI analysis")
            print("[Agent 7]   Install with: pip install openai")
            return {"status": "skipped", "reason": "openai package not installed"}

        client, settings = create_ai_client()
        if not client or not settings:
            print("[Agent 7] No OPENROUTER_API_KEY found — skipping AI analysis")
            return {"status": "skipped", "reason": "No API key"}

        # --- Load required state files ---
        try:
            draft_schedule = self._load_json(STATE_DIR / "05_draft_schedule.json")
        except FileNotFoundError:
            print("[Agent 7] state/05_draft_schedule.json not found — run Optimiser first")
            return {"status": "skipped", "reason": "Missing draft schedule"}

        try:
            metrics = self._load_json(STATE_DIR / "02_metrics.json")
        except FileNotFoundError:
            print("[Agent 7] state/02_metrics.json not found — run Analyst first")
            return {"status": "skipped", "reason": "Missing metrics"}

        scorecard: Optional[dict] = None
        scorecard_path = OUTPUT_DIR / "scorecard.json"
        if scorecard_path.exists():
            try:
                scorecard = self._load_json(scorecard_path)
            except Exception as e:
                print(f"[Agent 7] Warning: could not load scorecard.json: {e}")

        # --- Build structured context summaries ---
        location_summaries = self._build_location_summaries(draft_schedule, scorecard)
        high_performers = self._extract_high_performers(metrics)
        underperformers = self._extract_underperformers(metrics)
        top_concerns = self._extract_top_concerns(draft_schedule)
        trainer_load = self._summarise_trainer_load(draft_schedule)

        # --- Build prompt ---
        system_prompt = self._build_system_prompt()
        user_message = self._build_user_message(
            location_summaries,
            high_performers,
            underperformers,
            top_concerns,
            trainer_load,
        )

        # --- Call AI model ---
        try:
            print(f"[Agent 7] Calling {settings['model']} for schedule insights...")
            response = create_chat_completion(
                client=client,
                system_prompt=system_prompt,
                user_prompt=user_message,
                model=settings["model"],
                max_tokens=self.MAX_TOKENS,
            )
        except Exception as e:
            print(f"[Agent 7] Unexpected error calling AI model: {e} — skipping")
            return {"status": "skipped", "reason": str(e)}

        cache_stats = {
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "input_tokens": getattr(response.usage, "prompt_tokens", 0),
            "output_tokens": getattr(response.usage, "completion_tokens", 0),
        }
        print(
            f"[Agent 7] Tokens — input: {cache_stats['input_tokens']}, "
            f"output: {cache_stats['output_tokens']}, "
            f"cache_read: {cache_stats['cache_read_input_tokens']}, "
            f"cache_write: {cache_stats['cache_creation_input_tokens']}"
        )

        # --- Parse response ---
        raw_text = response.choices[0].message.content or ""
        parsed = self._parse_json_response(raw_text)
        if parsed is None:
            print("[Agent 7] Could not parse JSON response — saving raw text")
            fallback = {
                "status": "partial",
                "raw_response": raw_text,
                "cache_stats": cache_stats,
            }
            self._write_outputs(fallback)
            return fallback

        # --- Build structured output ---
        from datetime import date

        insights = {
            "status": "success",
            "generated_for_week": scorecard.get("generated_for_week", str(date.today()))
            if scorecard
            else str(date.today()),
            "model_used": settings["model"],
            "cache_stats": cache_stats,
            "schedule_assessment": parsed.get("schedule_assessment", {}),
            "swap_recommendations": parsed.get("swap_recommendations", []),
            "strategic_insights": parsed.get("strategic_insights", []),
            "trainer_workload_flags": parsed.get("trainer_workload_flags", []),
            "class_mix_verdict": parsed.get("class_mix_verdict", {}),
        }

        self._write_outputs(insights)

        swap_count = len(insights["swap_recommendations"])
        insight_count = len(insights["strategic_insights"])
        flag_count = len(insights["trainer_workload_flags"])
        print(
            f"[Agent 7] AI Advisor complete — "
            f"{swap_count} swap recommendations, "
            f"{insight_count} strategic insights, "
            f"{flag_count} trainer workload flags"
        )
        return insights

    # ------------------------------------------------------------------ helpers

    def _load_json(self, path: Path) -> dict:
        with open(path) as f:
            return json.load(f)

    def _write_outputs(self, data: dict) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        for dest in [STATE_DIR / "07_ai_insights.json", OUTPUT_DIR / "ai_insights.json"]:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "w") as f:
                json.dump(data, f, indent=2)

    def _build_location_summaries(
        self, draft_schedule: dict, scorecard: Optional[dict]
    ) -> List[dict]:
        summaries = []
        locations = draft_schedule.get("locations", {})
        for loc_name, loc_data in locations.items():
            slots = loc_data.get("schedule", [])
            total = len(slots)
            avg_fill = (
                sum(s.get("predicted_fill_rate", 0) for s in slots) / total
                if total
                else 0.0
            )
            violations = [
                v
                for s in slots
                for v in s.get("constraint_violations", [])
            ]
            class_counts: Dict[str, int] = {}
            trainer_counts: Dict[str, int] = {}
            for s in slots:
                cn = s.get("class_name", "Unknown")
                tr = s.get("trainer_1", "Unknown")
                class_counts[cn] = class_counts.get(cn, 0) + 1
                if tr:
                    trainer_counts[tr] = trainer_counts.get(tr, 0) + 1

            top_classes = sorted(class_counts.items(), key=lambda x: -x[1])[:5]
            top_trainers = sorted(trainer_counts.items(), key=lambda x: -x[1])[:5]

            sc_data = {}
            if scorecard:
                sc_data = scorecard.get("locations", {}).get(loc_name, {})

            summaries.append(
                {
                    "location": loc_name,
                    "total_classes": total,
                    "predicted_avg_fill_rate": round(avg_fill, 3),
                    "historical_avg_fill_rate": sc_data.get("historical_avg_fill_rate"),
                    "top_classes": top_classes,
                    "top_trainers": top_trainers,
                    "constraint_violations": violations[:10],
                    "soft_constraint_penalties": sc_data.get("soft_constraint_penalties", 0),
                }
            )
        return summaries

    def _extract_high_performers(self, metrics: dict) -> List[dict]:
        combos = metrics.get("class_trainer_slot_metrics", [])
        high = [
            {
                "location": r["location"],
                "class": r["class"],
                "trainer": r["trainer"],
                "day": r["day"],
                "time": r["time"],
                "avg_fill_rate": round(r.get("avg_fill_rate", 0), 3),
                "avg_checkin": round(r.get("avg_checkin", 0), 1),
                "session_count": r.get("session_count", 0),
            }
            for r in combos
            if r.get("avg_fill_rate", 0) >= 0.5
            and r.get("session_count", 0) >= 20
        ]
        high.sort(key=lambda x: -x["avg_fill_rate"])
        return high[:10]

    def _extract_underperformers(self, metrics: dict) -> List[dict]:
        combos = metrics.get("class_trainer_slot_metrics", [])
        low = [
            {
                "location": r["location"],
                "class": r["class"],
                "trainer": r["trainer"],
                "day": r["day"],
                "time": r["time"],
                "avg_fill_rate": round(r.get("avg_fill_rate", 0), 3),
                "avg_checkin": round(r.get("avg_checkin", 0), 1),
                "session_count": r.get("session_count", 0),
            }
            for r in combos
            if r.get("avg_fill_rate", 0) < 0.30
            and r.get("session_count", 0) >= 5
        ]
        low.sort(key=lambda x: x["avg_fill_rate"])
        return low[:5]

    def _extract_top_concerns(self, draft_schedule: dict) -> List[dict]:
        concerns = []
        locations = draft_schedule.get("locations", {})
        for loc_name, loc_data in locations.items():
            for slot in loc_data.get("schedule", []):
                violations = slot.get("constraint_violations", [])
                fill = slot.get("predicted_fill_rate", 1.0)
                if violations or fill < 0.25:
                    concerns.append(
                        {
                            "location": loc_name,
                            "day": slot.get("day_of_week"),
                            "time": slot.get("time"),
                            "class": slot.get("class_name"),
                            "trainer": slot.get("trainer_1"),
                            "predicted_fill": round(fill, 3),
                            "violations": violations,
                        }
                    )
        concerns.sort(key=lambda x: (len(x["violations"]) * -1, x["predicted_fill"]))
        return concerns[:15]

    def _summarise_trainer_load(self, draft_schedule: dict) -> List[dict]:
        trainer_days: Dict[str, Dict[str, int]] = {}
        locations = draft_schedule.get("locations", {})
        for loc_name, loc_data in locations.items():
            for slot in loc_data.get("schedule", []):
                t1 = slot.get("trainer_1", "")
                if not t1:
                    continue
                key = f"{t1}@{loc_name}"
                day = slot.get("day_of_week", "Unknown")
                if key not in trainer_days:
                    trainer_days[key] = {}
                trainer_days[key][day] = trainer_days[key].get(day, 0) + 1

        result = []
        for key, day_map in trainer_days.items():
            trainer, loc = key.split("@", 1)
            total = sum(day_map.values())
            max_day = max(day_map.values())
            result.append(
                {
                    "trainer": trainer,
                    "location": loc,
                    "total_classes_week": total,
                    "max_classes_single_day": max_day,
                    "days_worked": len(day_map),
                }
            )
        result.sort(key=lambda x: -x["total_classes_week"])
        return result

    def _build_system_prompt(self) -> str:
        return """You are an expert fitness studio schedule optimiser with deep knowledge of:
- Barre, PowerCycle, and functional fitness class dynamics
- Trainer performance management in boutique studios
- Revenue optimisation through schedule design
- Mumbai and Bengaluru fitness market patterns

You analyse weekly schedules for three Physique57 India studios:
1. Kwality House, Kemps Corner (Mumbai) — flagship
2. Supreme HQ, Bandra (Mumbai) — PowerCycle hub
3. Kenkere House (Bengaluru) — Barre-focused community studio

Key rules you must be aware of:
- Barre 57 family must be ≥ 25% of total weekly classes
- PowerCycle: never at Kenkere, ≥ 2/day at Supreme on weekdays
- Strength Lab: Kwality only, Atulan Purohit exclusively, Mon/Wed evenings
- No trainer > 3 consecutive classes or > 4/day
- Peak slots: 11:00–11:30 and 19:00–19:30 must never be empty
- Sunday: max 6 classes, nothing before 10:00, no evening band

You respond ONLY with valid JSON matching the requested schema. No preamble, no explanation outside the JSON."""

    def _build_user_message(
        self,
        location_summaries: List[dict],
        high_performers: List[dict],
        underperformers: List[dict],
        top_concerns: List[dict],
        trainer_load: List[dict],
    ) -> str:
        context = {
            "location_summaries": location_summaries,
            "top_10_high_confidence_performers": high_performers,
            "top_5_underperformers": underperformers,
            "top_concerns": top_concerns,
            "trainer_workload_summary": trainer_load[:20],
        }

        schema_description = """
Return a JSON object with exactly these keys:

{
  "schedule_assessment": {
    "Kwality House, Kemps Corner": "<2-3 sentence quality assessment>",
    "Supreme HQ, Bandra": "<2-3 sentence quality assessment>",
    "Kenkere House": "<2-3 sentence quality assessment>"
  },
  "swap_recommendations": [
    {
      "location": "string",
      "day": "string (e.g. Monday)",
      "time": "string (e.g. 09:00)",
      "current_class": "string",
      "current_trainer": "string",
      "suggested_class": "string",
      "suggested_trainer": "string",
      "reasoning": "string (1-2 sentences)",
      "confidence_pct": integer (0-100)
    }
  ],
  "strategic_insights": [
    "string (one insight per element, 5-6 total)"
  ],
  "trainer_workload_flags": [
    {
      "trainer": "string",
      "location": "string",
      "issue_type": "overuse OR underuse",
      "details": "string"
    }
  ],
  "class_mix_verdict": {
    "Kwality House, Kemps Corner": "string",
    "Supreme HQ, Bandra": "string",
    "Kenkere House": "string"
  }
}
"""

        return f"""Analyse the following studio schedule data and provide strategic insights.

## Schedule Data
{json.dumps(context, indent=2)}

## Instructions
{schema_description}

Provide 3-5 swap recommendations focused on high-impact changes.
Flag trainers with > 4 classes/day or > 14 classes/week as potential overuse.
Flag Tier 1 trainers with < 6 classes/week as potential underuse.
Strategic insights should address cross-location patterns and systemic opportunities.
"""

    def _parse_json_response(self, text: str) -> Optional[dict]:
        # Try direct parse first
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code block
        import re
        patterns = [
            r"```json\s*([\s\S]+?)\s*```",
            r"```\s*([\s\S]+?)\s*```",
            r"(\{[\s\S]+\})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    continue

        return None
