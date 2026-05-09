import json

with open("rules/trainer_profiles.json") as f:
    profiles = json.load(f)

for p in profiles:
    if p.get("qualifications", {}).get("powercycle"):
        print(f"{p['name']} (Tier {p.get('tier', 3)})")
