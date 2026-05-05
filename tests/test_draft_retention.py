import os
from pathlib import Path

from agents.draft_retention import prune_draft_schedule_files


def touch(path: Path, timestamp: int) -> None:
    path.write_text("{}")
    os.utime(path, (timestamp, timestamp))


def test_prune_draft_schedule_files_keeps_canonical_and_five_newest_groups(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    touch(state_dir / "05_draft_schedule.json", 1)

    for i in range(7):
        group = f"run{i}_abc{i}"
        timestamp = 100 + i
        touch(state_dir / f"05_draft_schedule_{group}.json", timestamp)
        touch(state_dir / f"05_draft_schedule_{group}_max_score.json", timestamp)
        touch(state_dir / f"05_draft_schedule_{group}_trainer_hours.json", timestamp)
        touch(state_dir / f"05_draft_schedule_{group}_class_variety.json", timestamp)

    deleted = prune_draft_schedule_files(state_dir, keep_groups=5)
    remaining = {path.name for path in state_dir.glob("05_draft_schedule*.json")}

    assert state_dir / "05_draft_schedule.json" not in deleted
    assert "05_draft_schedule.json" in remaining
    assert not any("run0_abc0" in name for name in remaining)
    assert not any("run1_abc1" in name for name in remaining)
    for i in range(2, 7):
        assert f"05_draft_schedule_run{i}_abc{i}.json" in remaining
        assert f"05_draft_schedule_run{i}_abc{i}_trainer_hours.json" in remaining
    assert len(remaining) == 1 + (5 * 4)


def test_prune_draft_schedule_files_treats_named_verification_outputs_as_groups(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    touch(state_dir / "05_draft_schedule.json", 1)
    touch(state_dir / "05_draft_schedule_score_breakdown_verify.json", 10)
    touch(state_dir / "05_draft_schedule_score_breakdown_verify_max_score.json", 10)
    touch(state_dir / "05_draft_schedule_score_floor_verify.json", 20)

    prune_draft_schedule_files(state_dir, keep_groups=1)
    remaining = {path.name for path in state_dir.glob("05_draft_schedule*.json")}

    assert remaining == {
        "05_draft_schedule.json",
        "05_draft_schedule_score_floor_verify.json",
    }
