import json

with open("rules/trainer_profiles.json") as f:
    data = json.load(f)

for p in data:
    tier = p.get("tier")
    print(f"{p['name']}: tier {tier} type {type(tier)}")
