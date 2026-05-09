import csv
with open("outputs/schedule_supreme_detailed.csv") as f:
    reader = csv.DictReader(f)
    for row in reader:
        trainer = row.get("Trainer 1")
        if trainer in ["Richard D'Costa", "Bret Saldanha", "Simonelle De Vitre", "Anmol Sharma"]:
            print(f"{row['Date']} {row['Time']} {trainer} -> {row['Scheduling Reason']}")
