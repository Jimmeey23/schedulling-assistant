import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from rule_config import get_active_hard_rule_groups

STATE_DIR = Path("state")
RULES_DIR = Path("rules")


@dataclass
class HardConstraint:
    constraint_id: str
    type: str
    location: Optional[str]
    description: str


@dataclass
class SoftConstraint:
    constraint_id: str
    priority: int
    description: str
    penalty: float


SOFT_CONSTRAINTS = [
    SoftConstraint("MIX-001", 1, "Barre 57 family 45-55% of weekly classes", 10.0),
    SoftConstraint("MIX-002", 1, "PowerCycle 8-10% Kwality, 25-28% Supreme, 0% Kenkere", 8.0),
    SoftConstraint("MIX-003", 2, "Mat 57 minimum 3-4x per week per location", 5.0),
    SoftConstraint("MIX-004", 2, "FIT 1x daily morning slots", 5.0),
    SoftConstraint("MIX-005", 2, "Foundations at Kenkere 1x daily; Kwality/Supreme 2-3x/week", 5.0),
    SoftConstraint("MIX-006", 3, "Recovery weekends only afternoon slots 12:30-16:00", 4.0),
    SoftConstraint("MIX-007", 3, "Back Body Blaze morning only 07:30-09:00 max 3x/week", 4.0),
    SoftConstraint("MIX-008", 3, "Studio Amped Up max 1-2x/week Reshma or Rohan only", 4.0),
    SoftConstraint("MIX-009", 4, "Cardio Barre 1x/day evenings preferred", 3.0),
    SoftConstraint("MIX-010", 4, "Strength Lab max 2x/week Kwality Mon/Wed evenings only", 3.0),
    SoftConstraint("SLOT-001", 1, "Always fill peak midday 11:00-11:30 and prime evening 19:00-19:30", 8.0),
    SoftConstraint("SLOT-002", 2, "Fill morning block 08:00-09:30 and early evening 17:45-18:15", 5.0),
    SoftConstraint("SLOT-003", 3, "Fill early morning 07:00-07:30 and late evening 20:00", 3.0),
    SoftConstraint("SLOT-004", 4, "Mid-afternoon 13:00-16:00 optional", 2.0),
]


class RuleEngine:
    def run(self) -> dict:
        print("[Agent 4] Rule Engine starting...")

        active_groups = get_active_hard_rule_groups()

        hard_constraints = []

        for rule in active_groups.get("universal", []):
            hard_constraints.append(
                asdict(
                    HardConstraint(
                        constraint_id=rule["id"],
                        type=rule["type"],
                        location=None,
                        description=rule["description"],
                    )
                )
            )

        for group_rules, loc_name in [
            (active_groups.get("location_kwality", []), "Kwality House, Kemps Corner"),
            (active_groups.get("location_supreme", []), "Supreme HQ, Bandra"),
            (active_groups.get("location_kenkere", []), "Kenkere House"),
        ]:
            for rule in group_rules:
                hard_constraints.append(
                    asdict(
                        HardConstraint(
                            constraint_id=rule["id"],
                            type=rule["type"],
                            location=loc_name,
                            description=rule["description"],
                        )
                    )
                )

        soft_constraints = [asdict(s) for s in SOFT_CONSTRAINTS]

        output = {
            "hard_constraints": hard_constraints,
            "soft_constraints": soft_constraints,
        }

        STATE_DIR.mkdir(exist_ok=True)
        with open(STATE_DIR / "04_constraints.json", "w") as f:
            json.dump(output, f, indent=2)

        print(
            f"[Agent 4] Rule Engine complete — {len(hard_constraints)} hard constraints, "
            f"{len(soft_constraints)} soft constraints loaded"
        )
        return output
