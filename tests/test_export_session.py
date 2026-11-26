import json
import sqlite3
import tarfile
import time
from contextlib import closing
from pathlib import Path

import pytest

from agentrouter_prober.browser_session import BrowserSessionError, ZenSessionLoader
from agentrouter_prober.export_session import export_session
from test_browser_session import make_profile


@pytest.mark.parametrize("expiry_scale", [1, 1000])
def test_export_reads_live_wal_and_only_copies_matching_agentrouter_login(
    state_directory, expiry_scale
):
    profile = state_directory / "source"
    profile.mkdir()
    attributes = "^userContextId=6"
    make_profile(profile, origin_attributes=attributes)
    expiry_seconds = int(time.time()) + 3600
    with closing(sqlite3.connect(profile / "cookies.sqlite")) as connection:
        connection.execute("pragma journal_mode = WAL")
        connection.execute("pragma wal_autocheckpoint = 0")
        connection.execute(
            "update moz_cookies set value = ?, expiry = ?, lastAccessed = ?",
            ("session-from-live-wal", expiry_seconds * expiry_scale, 200),
        )
        connection.execute(
            "insert into moz_cookies values (?,?,?,?,?,?)",
            ("other.example", "session", "unrelated-login", expiry_seconds, 300, ""),
        )
        connection.execute(
            "insert into moz_cookies values (?,?,?,?,?,?)",
            ("agentrouter.org", "analytics", "unrelated-cookie", expiry_seconds, 400, attributes),
        )
        connection.commit()
        assert (profile / "cookies.sqlite-wal").is_file()
        result = export_session(profile, state_directory / "session.tar.gz", "https://agentrouter.org")

    assert result.expires_at.timestamp() == expiry_seconds
    extracted = state_directory / "extracted"
    with tarfile.open(result.archive) as archive:
        files = [member for member in archive.getmembers() if member.isfile()]
        assert {member.name for member in files} == {
            "zen-profile/cookies.sqlite",
            "zen-profile/storage/default/https+++agentrouter.org^userContextId=6/ls/data.sqlite",
        }
        assert all(member.mode == 0o600 and member.uid == member.gid == 10001 for member in files)
        for member in files:
            target = extracted / Path(member.name)
            assert target.resolve().is_relative_to(extracted.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source:
                target.write_bytes(source.read())
    exported_profile = extracted / "zen-profile"
    session = ZenSessionLoader(exported_profile, "https://agentrouter.org").load()
    assert session.cookie == "session-from-live-wal"
    assert session.user_id == 60923 and session.origin_attributes == attributes
    with closing(sqlite3.connect(exported_profile / "cookies.sqlite")) as connection:
        assert connection.execute("select count(*) from moz_cookies").fetchone()[0] == 1
        assert connection.execute("select expiry from moz_cookies").fetchone()[0] == expiry_seconds * expiry_scale
        assert connection.execute("pragma journal_mode").fetchone()[0] == "delete"
    storage = exported_profile / "storage/default/https+++agentrouter.org^userContextId=6/ls/data.sqlite"
    with closing(sqlite3.connect(storage)) as connection:
        rows = connection.execute("select key, value from data").fetchall()
        assert len(rows) == 1 and rows[0][0] == "user"
        assert json.loads(rows[0][1]) == {"id": 60923}


def test_export_requires_a_matching_logged_in_user(state_directory):
    profile = state_directory / "source"
    profile.mkdir()
    make_profile(profile, with_user=False)
    output = state_directory / "session.tar.gz"
    with pytest.raises(BrowserSessionError, match="No logged-in AgentRouter session"):
        export_session(profile, output, "https://agentrouter.org")
    assert not output.exists()


def test_export_does_not_overwrite_original_profile(state_directory):
    make_profile(state_directory)
    original = (state_directory / "cookies.sqlite").read_bytes()
    with pytest.raises(BrowserSessionError, match="outside the original Zen profile"):
        export_session(state_directory, state_directory / "cookies.sqlite", "https://agentrouter.org")
    assert (state_directory / "cookies.sqlite").read_bytes() == original
