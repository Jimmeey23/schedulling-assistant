import pandas as pd
import json

def analyze_trainer_days():
    df = pd.read_csv('Sessions Performance Data.csv')
    # Assuming 'Trainer 1' and 'Day' (or similar) are columns
    # Let's check columns first
    print("Columns:", df.columns.tolist())
    
    # We need Day and Trainer name
    # Common columns: 'Trainer 1', 'Day'
    trainer_col = 'Trainer 1'
    day_col = 'Day'
    
    if trainer_col not in df.columns or day_col not in df.columns:
        # Try finding them
        for col in df.columns:
            if 'trainer' in col.lower(): trainer_col = col
            if 'day' in col.lower(): day_col = col
            
    print(f"Using columns: {trainer_col}, {day_col}")
    
    # Count sessions per trainer per day
    counts = df.groupby([trainer_col, day_col]).size().unstack(fill_value=0)
    
    # Standard 7 days
    all_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    
    # Ensure all days are present (some might be missing from data)
    for d in all_days:
        if d not in counts.columns:
            counts[d] = 0
            
    counts = counts[all_days]
    
    results = {}
    for trainer, row in counts.iterrows():
        # Find 2 days with lowest session counts
        sorted_days = row.sort_values()
        off_days = sorted_days.index[:2].tolist()
        work_days = sorted_days.index[2:].tolist()
        # Sort work_days to original order
        work_days = [d for d in all_days if d in work_days]
        results[trainer] = {
            "off_days": off_days,
            "work_days": work_days,
            "counts": row.to_dict()
        }
        
    return results

if __name__ == "__main__":
    try:
        data = analyze_trainer_days()
        with open('scratch/trainer_historical_days.json', 'w') as f:
            json.dump(data, f, indent=2)
        print("Analysis complete. Saved to scratch/trainer_historical_days.json")
    except Exception as e:
        print(f"Error: {e}")
