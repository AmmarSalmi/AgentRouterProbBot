import json
import os
import stat

import pytest

from agentrouter_prober.state import StateStore, health_is_fresh


@pytest.mark.parametrize("contents", [b'{"models": []}', b"not json", b"\xff"])
def test_healthcheck_leaves_corrupt_state_for_startup_recovery(state_directory, contents):
    path = state_directory / "state.json"
    path.write_bytes(contents)
    store = StateStore(path)

    assert not health_is_fresh(path, 3600)
    assert path.read_bytes() == contents
    assert not list(state_directory.glob("state.json.corrupt-*"))

    state = store.load()
    assert state.last_error == "Existing state file was unreadable"
    assert not path.exists()
    backup, = state_directory.glob("state.json.corrupt-*")
    assert backup.read_bytes() == contents
    store.save(state)
    assert store.load() == state
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_invalid_health_timestamp_and_legacy_model_fields_are_ignored(state_directory):
    path = state_directory / "state.json"
    path.write_text(json.dumps({
        "version": 1,
        "models": {"model": {"availability": "available", "confirm_failures": 3}},
        "last_successful_status_at": "bad timestamp",
    }))
    store = StateStore(path)

    assert not health_is_fresh(path, 3600)
    state = store.load()
    assert state.models["model"].availability == "available"
    store.save(state)
    assert "confirm_failures" not in json.loads(path.read_text())["models"]["model"]
