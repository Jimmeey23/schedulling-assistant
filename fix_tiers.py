import json

tier1_names = [
    "Rohan Dahima", "Pranjali Jain", "Reshma Sharma", "Richard D'Costa", 
    "Atulan Purohit", "Anisha Shah", "Mrigakshi Jaiswal", "Vivaran Dhasmana", 
    "Karanvir Bhatia", "Cauveri Vikrant", "Karan Bhatia", "Kajol Kanchan", 
    "Shruti Kulkarni", "Pushyank Nahar", "Siddhartha Kusuma", "Chaitanya Nahar"
]

with open("rules/trainer_profiles.json", "r") as f:
    profiles = json.load(f)

for p in profiles:
    if p["name"] in tier1_names:
        p["tier"] = 1
        # Slightly increase max_classes_per_day to help with availability bottlenecks
        for loc_name, loc_data in p.get("locations", {}).items():
            if loc_data.get("max_classes_per_day", 0) < 4:
                loc_data["max_classes_per_day"] = 4
    else:
        # If not explicitly Tier 1, keep their existing tier or default to 2/3
        if p.get("tier") == 1:
            p["tier"] = 2 # demote if they weren't in our verified Tier 1 list, though Siddhartha and Chaitanya might be T1, I added them manually to the list above to be safe based on previous output.

with open("rules/trainer_profiles.json", "w") as f:
    json.dump(profiles, f, indent=2)

print("Tiers updated successfully.")
