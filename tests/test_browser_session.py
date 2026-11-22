import json
import sqlite3
import time

import pytest

from agentrouter_prober.browser_session import BrowserSessionError, ZenSessionLoader


def _varint(value):
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _snappy_literal(data):
    length = len(data)
    encoded = length - 1
    if encoded < 60:
        tag = bytes([encoded << 2])
    else:
        count = max(1, (encoded.bit_length() + 7) // 8)
        tag = bytes([(59 + count) << 2]) + encoded.to_bytes(count, "little")
    return _varint(length) + tag + data


def make_profile(root, *, with_user=True):
    cookie_db = sqlite3.connect(root / "cookies.sqlite")
    cookie_db.execute(
        "create table moz_cookies (host text, name text, value text, expiry integer, "
        "lastAccessed integer, originAttributes text)"
    )
    cookie_db.execute(
        "insert into moz_cookies values (?,?,?,?,?,?)",
        (
            "agentrouter.org",
            "session",
            "secret-session",
            int(time.time()) + 3600,
            100,
            "",
        ),
    )
    cookie_db.commit()
    cookie_db.close()

    storage = root / "storage" / "default" / "https+++agentrouter.org" / "ls"
    storage.mkdir(parents=True)
    local = sqlite3.connect(storage / "data.sqlite")
    local.execute(
        "create table data (key text, conversion_type integer, compression_type integer, value blob)"
    )
    if with_user:
        payload = json.dumps({"id": 60923, "username": "test"}).encode()
        local.execute(
            "insert into data values (?,?,?,?)",
            ("user", 1, 1, _snappy_literal(payload)),
        )
    local.commit()
    local.close()


def test_zen_loader_matches_cookie_to_compressed_local_storage(state_directory):
    make_profile(state_directory)
    session = ZenSessionLoader(state_directory, "https://agentrouter.org").load()
    assert session.cookie == "secret-session"
    assert session.user_id == 60923
    assert session.origin_attributes == ""


def test_zen_loader_requires_logged_in_user_cache(state_directory):
    make_profile(state_directory, with_user=False)
    with pytest.raises(BrowserSessionError, match="No logged-in AgentRouter session"):
        ZenSessionLoader(state_directory, "https://agentrouter.org").load()
