import json

with open("rules/trainer_profiles.json") as f:
    profiles = json.load(f)

for p in profiles:
    if p.get("tier", 3) == 1:
        locs = p.get("locations", {})
        for loc_name, loc_data in locs.items():
            days = loc_data.get("available_days", [])
            window = loc_data.get("time_window", {})
            end = window.get("end", "22:00")
            if "Tuesday" in days and end >= "19:00":
                print(f"{p['name']} is available at {loc_name} on Tuesday until {end}")
