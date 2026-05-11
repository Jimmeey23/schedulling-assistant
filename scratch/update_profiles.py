import json

def update_profiles():
    with open('rules/trainer_profiles.json') as f:
        profiles = json.load(f)
    
    with open('scratch/trainer_historical_days.json') as f:
        historical = json.load(f)
    
    # Normalize names in historical data (strip spaces)
    normalized_historical = {k.strip(): v for k, v in historical.items()}
    
    updated_count = 0
    for p in profiles:
        name = p['name'].strip()
        hist = normalized_historical.get(name)
        
        if not hist:
            # Try fuzzy match or skip
            # Some names have double spaces in historical data like "Kajol  Kanchan"
            for k, v in normalized_historical.items():
                if k.replace("  ", " ") == name.replace("  ", " "):
                    hist = v
                    break
        
        if hist:
            work_days = hist['work_days']
            # Update each location
            for loc, data in p.get('locations', {}).items():
                data['available_days'] = work_days
            updated_count += 1
        else:
            print(f"Warning: No historical data for {name}")

    with open('rules/trainer_profiles.json', 'w') as f:
        json.dump(profiles, f, indent=2)
    
    print(f"Updated {updated_count} trainer profiles.")

if __name__ == "__main__":
    update_profiles()
