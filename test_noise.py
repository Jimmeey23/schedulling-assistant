from agents.optimiser import ScheduleOptimiser
import json
import csv

opt1 = ScheduleOptimiser("2026-05-11", ["Supreme HQ, Bandra"], variation_seed=123)
opt2 = ScheduleOptimiser("2026-05-11", ["Supreme HQ, Bandra"], variation_seed=999)

out1 = opt1.run()
out2 = opt2.run()

for i, (s1, s2) in enumerate(zip(out1["schedule"], out2["schedule"])):
    if s1["trainer_1"] != s2["trainer_1"]:
        print(f"Slot {i}: Seed 1 picked {s1['trainer_1']}, Seed 2 picked {s2['trainer_1']}")
    
