import json

def refine_update():
    with open('rules/trainer_profiles.json') as f:
        profiles = json.load(f)
    
    with open('scratch/trainer_historical_days.json') as f:
        historical = json.load(f)
        
    normalized_historical = {k.strip(): v for k, v in historical.items()}
    all_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    
    updated_count = 0
    for p in profiles:
        name = p['name'].strip()
        hist = normalized_historical.get(name)
        
        if not hist:
            for k, v in normalized_historical.items():
                if k.replace("  ", " ") == name.replace("  ", " "):
                    hist = v
                    break
        
        if not hist:
            continue
            
        # Get all days this trainer owns across all locations
        owned_days = set()
        for loc_data in p.get('locations', {}).values():
            for block in loc_data.get('owned_blocks', []):
                day = block.get('day')
                if day in all_days:
                    owned_days.add(day)
        
        counts = hist['counts']
        # Sort days by counts descending
        sorted_days = sorted(all_days, key=lambda d: counts.get(d, 0), reverse=True)
        
        # Start with owned days
        work_days = list(owned_days)
        
        # Add days from sorted_days until we have 5, or until we've exhausted sorted_days
        for d in sorted_days:
            if len(work_days) >= 5:
                break
            if d not in work_days:
                work_days.append(d)
        
        # Re-sort work_days to standard order
        work_days = [d for d in all_days if d in work_days]
        
        # Update each location
        for loc, data in p.get('locations', {}).items():
            data['available_days'] = work_days
        
        updated_count += 1

    with open('rules/trainer_profiles.json', 'w') as f:
        json.dump(profiles, f, indent=2)
    
    print(f"Refined {updated_count} trainer profiles with owned-block protection.")

if __name__ == "__main__":
    refine_update()
