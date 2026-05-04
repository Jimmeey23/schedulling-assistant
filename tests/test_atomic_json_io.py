import json

import pytest

from agents.io_utils import atomic_write_json


def test_atomic_write_json_writes_readable_json(tmp_path):
    path = tmp_path / "state.json"

    atomic_write_json(path, {"schedule": [{"class_name": "Studio Barre 57"}]})

    assert json.loads(path.read_text()) == {
        "schedule": [{"class_name": "Studio Barre 57"}]
    }


def test_atomic_write_json_does_not_replace_existing_file_when_verification_fails(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"previous": True}))

    def write_invalid(data, file_obj, **kwargs):
        file_obj.write('{"broken": "\x00"}')

    monkeypatch.setattr("agents.io_utils.json.dump", write_invalid)

    with pytest.raises(ValueError):
        atomic_write_json(path, {"previous": False})

    assert json.loads(path.read_text()) == {"previous": True}
