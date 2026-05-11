# Scheduler Team Canonical Rulebook

This document is intentionally short. The live scheduling source of truth is:

1. `config/schedule_config.json` for targets, saved custom rules, manual pins, AI settings, trainer priorities, and generation guardrails.
2. `rules/trainer_profiles.json` for trainer availability, certifications, active status, tiers, historic week-off defaults, and location access.
3. `rules/class_formats.json` for class format metadata, eligible locations, and certification families.
4. `config/rules_config.json` plus `rules/*_rules.json` for universal/location guardrail descriptions only.

Do not add hard trainer ownership rules to static rule files unless the same rule is saved in Settings as a hard custom rule.

## Weekly Assignment Floors

These floors are hard generation requirements:

- Kwality House, Kemps Corner: minimum 70 assignments per week.
- Supreme HQ, Bandra: minimum 65 assignments per week.
- Kenkere House: minimum 55 assignments per week.

The optimizer may exceed these floors within saved daily max caps when high-performing, qualified trainer/class/room combinations are available.

## Trainer Utilisation

- Tier 1 trainers are the first-choice capacity pool.
- Tier 1 trainers should be pulled toward 15h/week where feasible, without exceeding the hard 15h cap.
- No trainer may exceed 4 assigned hours in one day.
- No trainer may be assigned to both AM and PM shifts on the same day.
- No trainer may work more than one location in the same shift.
- Every trainer must have at least one weekly off day; two is preferred when coverage allows.
- Saved leave, dated off-days, inactive status, assignment days, and custom hard trainer rules are binding.

Priority trainer pool:

- Anisha Shah
- Rohan Dahima
- Reshma Sharma
- Atulan Purohit
- Pranjali Jain
- Karanvir Bhatia
- Mrigakshi Jaiswal
- Vivaran Dhasmana
- Pushyank Nahar
- Kajol Kanchan
- Shruti Kulkarni

## Format-Specific Priority

Mumbai PowerCycle priority:

- Vivaran Dhasmana
- Cauveri Vikrant
- Karanvir Bhatia

Strength Lab and FIT priority:

- Atulan Purohit
- Mrigakshi Jaiswal
- Anisha Shah
- Reshma Sharma
- Richard D'Costa

These are score priorities, not absolute hard ownership rules. Certification, availability, leave, room availability, daily hour cap, weekly cap, and quality gates still win.

## Quality Gate

The app exists to remove proven weak performers from generated schedules.

Do not schedule a proven low-performing class/trainer/slot history unless it is manually pinned. A proven weak option is any repeated-history option below either threshold:

- Average check-ins below 3.0.
- Fill rate below 22%.

Non-pinned generated rows below 50/100 score should not be accepted as optimized output.

## Mumbai Peak Clusters

For Kwality House and Supreme HQ, prioritize parallel-room use in these clusters where room and trainer constraints allow:

- 08:00 / 08:15 / 08:30 / 08:45
- 11:00 / 11:15 / 11:30 / 11:45
- 18:00 / 18:15 / 18:30 / 18:45

Do not collapse demand into only one start time when qualified trainers and rooms are available.

## Location Format Guardrails

- PowerCycle is allowed only in Mumbai studios and never at Kenkere House.
- Strength Lab is allowed only at Kwality House.
- Pre/Post Natal is not auto-scheduled unless explicitly saved as a manual pin or hard custom rule.
- Specialist formats require matching trainer certification.

## Persistence

Settings Console saves must write to `config/schedule_config.json` and `rules/trainer_profiles.json`. Saved custom rules, pins, targets, AI settings, priorities, leave, and off-days should survive process restarts and browser sessions.
