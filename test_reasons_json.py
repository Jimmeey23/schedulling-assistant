import json

with open("state/05_draft_schedule.json") as f:
    draft = json.load(f)

for s in draft.get("schedule", []):
    t = s.get("trainer_1")
    if t in ["Richard D'Costa", "Bret Saldanha", "Simonelle De Vitre", "Anmol Sharma"]:
        print(f"{s['day_of_week']} {s['time']} {t} -> {s.get('scheduling_reason')}")
