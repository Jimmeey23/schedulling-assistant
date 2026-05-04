import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any, *, indent: int | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())

        with tmp_path.open() as f:
            json.load(f)

        os.replace(tmp_path, path)
    except Exception as exc:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        if isinstance(exc, json.JSONDecodeError):
            raise ValueError(f"Atomic JSON write verification failed for {path}") from exc
        raise
