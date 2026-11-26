from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class BrowserSessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentRouterSession:
    cookie: str
    user_id: int
    origin_attributes: str = ""


def _read_varint(data: bytes, position: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    while position < len(data) and shift <= 63:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, position
        shift += 7
    raise ValueError("invalid Snappy length")


def _copy_snappy(out: bytearray, offset: int, length: int) -> None:
    if offset <= 0 or offset > len(out):
        raise ValueError("invalid Snappy copy offset")
    for _ in range(length):
        out.append(out[-offset])


def _snappy_decompress(data: bytes) -> bytes:
    """Decode the raw Snappy format used by Firefox local storage."""
    expected, position = _read_varint(data)
    out = bytearray()
    while position < len(data) and len(out) < expected:
        tag = data[position]
        position += 1
        kind = tag & 0x03
        if kind == 0:
            encoded_length = tag >> 2
            if encoded_length < 60:
                length = encoded_length + 1
            else:
                byte_count = encoded_length - 59
                if position + byte_count > len(data):
                    raise ValueError("truncated Snappy literal length")
                length = int.from_bytes(
                    data[position : position + byte_count], "little"
                ) + 1
                position += byte_count
            if position + length > len(data):
                raise ValueError("truncated Snappy literal")
            out.extend(data[position : position + length])
            position += length
        elif kind == 1:
            if position >= len(data):
                raise ValueError("truncated Snappy copy")
            length = 4 + ((tag >> 2) & 0x07)
            offset = ((tag & 0xE0) << 3) | data[position]
            position += 1
            _copy_snappy(out, offset, length)
        elif kind == 2:
            if position + 2 > len(data):
                raise ValueError("truncated Snappy copy")
            length = 1 + (tag >> 2)
            offset = int.from_bytes(data[position : position + 2], "little")
            position += 2
            _copy_snappy(out, offset, length)
        else:
            if position + 4 > len(data):
                raise ValueError("truncated Snappy copy")
            length = 1 + (tag >> 2)
            offset = int.from_bytes(data[position : position + 4], "little")
            position += 4
            _copy_snappy(out, offset, length)
    if len(out) != expected:
        raise ValueError("Snappy output length mismatch")
    return bytes(out)


def _sqlite_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _expiry_seconds(raw: int) -> float:
    # Firefox normally stores seconds. Current Zen builds use milliseconds.
    return raw / 1000 if raw > 100_000_000_000 else float(raw)


class ZenSessionLoader:
    def __init__(self, profile_path: Path, base_url: str):
        self.profile_path = profile_path
        parsed = urlparse(base_url)
        if not parsed.hostname:
            raise BrowserSessionError("AgentRouter URL has no host")
        self.host = parsed.hostname
        port = f"+{parsed.port}" if parsed.port else ""
        self.storage_origin = f"{parsed.scheme}+++{self.host}{port}"
        self._cached: AgentRouterSession | None = None
        self._cached_expires_at: float | None = None

    def _usable_cached_session(self) -> AgentRouterSession | None:
        if self._cached_expires_at is not None and self._cached_expires_at <= time.time():
            self._cached = None
            self._cached_expires_at = None
        return self._cached

    def _user_id(self, origin_attributes: str) -> int | None:
        storage = (
            self.profile_path
            / "storage"
            / "default"
            / f"{self.storage_origin}{origin_attributes}"
            / "ls"
            / "data.sqlite"
        )
        try:
            with closing(_sqlite_read_only(storage)) as connection:
                row = connection.execute(
                    "select conversion_type, compression_type, value "
                    "from data where key = 'user'"
                ).fetchone()
        except (OSError, sqlite3.Error, FileNotFoundError):
            return None
        if row is None:
            return None

        conversion_type, compression_type, raw = row
        if isinstance(raw, str):
            text = raw
        else:
            data = bytes(raw)
            try:
                if compression_type == 1:
                    data = _snappy_decompress(data)
                elif compression_type not in (0, None):
                    return None
                encoding = "utf-8" if conversion_type in (1, None) else "utf-16-le"
                text = data.decode(encoding)
            except (UnicodeError, ValueError):
                return None
        try:
            user = json.loads(text)
        except (TypeError, ValueError):
            return None
        user_id = user.get("id") if isinstance(user, dict) else None
        return user_id if type(user_id) is int and user_id > 0 else None

    def load(self) -> AgentRouterSession:
        cached = self._usable_cached_session()
        cookie_db = self.profile_path / "cookies.sqlite"
        try:
            with closing(_sqlite_read_only(cookie_db)) as connection:
                rows = connection.execute(
                    "select value, expiry, lastAccessed, originAttributes "
                    "from moz_cookies where host in (?, ?) and name = 'session'",
                    (self.host, f".{self.host}"),
                ).fetchall()
        except FileNotFoundError as exc:
            if cached is not None:
                return cached
            raise BrowserSessionError(
                f"Zen cookie database was not found in {self.profile_path}"
            ) from exc
        except (OSError, sqlite3.Error) as exc:
            if cached is not None:
                return cached
            raise BrowserSessionError("Zen cookie database could not be read") from exc

        now = time.time()
        candidates = sorted(rows, key=lambda row: (row[2], row[1]), reverse=True)
        for cookie, expiry, _, origin_attributes in candidates:
            if not isinstance(cookie, str) or not cookie:
                continue
            if type(expiry) is int and expiry > 0 and _expiry_seconds(expiry) <= now:
                continue
            attributes = origin_attributes if isinstance(origin_attributes, str) else ""
            user_id = self._user_id(attributes)
            if user_id is None:
                continue
            self._cached = AgentRouterSession(cookie, user_id, attributes)
            self._cached_expires_at = (
                _expiry_seconds(expiry) if type(expiry) is int and expiry > 0 else None
            )
            return self._cached

        if cached is not None:
            return cached
        raise BrowserSessionError(
            "No logged-in AgentRouter session matching Zen local storage was found; "
            "open AgentRouter in the configured Zen profile and log in"
        )
