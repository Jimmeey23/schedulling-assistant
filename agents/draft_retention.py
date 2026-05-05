from pathlib import Path
from typing import Dict, List


DRAFT_PREFIX = "05_draft_schedule_"
DRAFT_VARIANT_SUFFIXES = ("_max_score", "_trainer_hours", "_class_variety", "_fallback")


def _draft_group_name(path: Path) -> str | None:
    stem = path.stem
    if stem == "05_draft_schedule":
        return None
    if not stem.startswith(DRAFT_PREFIX):
        return None
    group = stem[len(DRAFT_PREFIX):]
    for suffix in DRAFT_VARIANT_SUFFIXES:
        if group.endswith(suffix):
            return group[: -len(suffix)]
    return group


def prune_draft_schedule_files(state_dir: Path, keep_groups: int = 5) -> List[Path]:
    """Keep canonical latest draft and only the newest draft run groups."""
    if keep_groups < 1 or not state_dir.exists():
        return []

    grouped: Dict[str, List[Path]] = {}
    for path in state_dir.glob("05_draft_schedule*.json"):
        group = _draft_group_name(path)
        if group is None:
            continue
        grouped.setdefault(group, []).append(path)

    newest_groups = sorted(
        grouped,
        key=lambda group: max(path.stat().st_mtime for path in grouped[group]),
        reverse=True,
    )
    keep = set(newest_groups[:keep_groups])
    deleted: List[Path] = []
    for group, paths in grouped.items():
        if group in keep:
            continue
        for path in paths:
            try:
                path.unlink()
                deleted.append(path)
            except FileNotFoundError:
                continue
    return deleted
