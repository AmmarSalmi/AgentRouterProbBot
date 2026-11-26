from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import tarfile
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .browser_session import (
    BrowserSessionError,
    ZenSessionLoader,
    _expiry_seconds,
    _sqlite_read_only,
)
from .config import _discover_zen_profile


@dataclass(frozen=True)
class SessionExport:
    archive: Path
    expires_at: datetime | None


def export_session(profile: Path, output: Path, base_url: str) -> SessionExport:
    profile = profile.expanduser().resolve()
    output = output.expanduser().resolve()
    if output.is_relative_to(profile):
        raise BrowserSessionError("Export the session outside the original Zen profile")
    loader = ZenSessionLoader(profile, base_url)
    session = loader.load()
    if any(character in session.origin_attributes for character in ("/", "\\", "\0")):
        raise BrowserSessionError("Zen session has invalid origin attributes")

    with closing(_sqlite_read_only(profile / "cookies.sqlite")) as connection:
        row = connection.execute(
            "select expiry, lastAccessed from moz_cookies "
            "where host in (?, ?) and name = 'session' and value = ? "
            "and coalesce(originAttributes, '') = ? "
            "order by lastAccessed desc limit 1",
            (loader.host, f".{loader.host}", session.cookie, session.origin_attributes),
        ).fetchone()
    if row is None:
        raise BrowserSessionError("The Zen session changed during export; try again")
    expiry, last_accessed = row
    expires_at = (
        datetime.fromtimestamp(_expiry_seconds(expiry), timezone.utc)
        if type(expiry) is int and expiry > 0
        else None
    )
    if expires_at is not None and expires_at.timestamp() <= time.time():
        raise BrowserSessionError("The Zen session expired during export; log in again")

    with closing(sqlite3.connect(":memory:")) as cookies:
        cookies.execute(
            "create table moz_cookies (host text, name text, value text, expiry integer, "
            "lastAccessed integer, originAttributes text)"
        )
        cookies.execute(
            "insert into moz_cookies values (?,?,?,?,?,?)",
            (loader.host, "session", session.cookie, expiry, last_accessed, session.origin_attributes),
        )
        cookies.commit()
        cookie_image = cookies.serialize()

    with closing(sqlite3.connect(":memory:")) as storage:
        storage.execute(
            "create table data (key text, conversion_type integer, "
            "compression_type integer, value blob)"
        )
        storage.execute(
            "insert into data values (?,?,?,?)",
            ("user", 1, 0, json.dumps({"id": session.user_id}).encode("utf-8")),
        )
        storage.commit()
        storage_image = storage.serialize()

    root = PurePosixPath("zen-profile")
    local = root / "storage" / "default" / f"{loader.storage_origin}{session.origin_attributes}" / "ls"
    files = {root / "cookies.sqlite": cookie_image, local / "data.sqlite": storage_image}
    directories = {parent for path in files for parent in path.parents if parent != PurePosixPath(".")}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as destination:
            with tarfile.open(fileobj=destination, mode="w:gz") as archive:
                for path in sorted(directories, key=lambda item: (len(item.parts), str(item))):
                    info = tarfile.TarInfo(path.as_posix())
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o700
                    info.uid = info.gid = 10001
                    info.mtime = int(time.time())
                    archive.addfile(info)
                for path, data in files.items():
                    info = tarfile.TarInfo(path.as_posix())
                    info.size = len(data)
                    info.mode = 0o600
                    info.uid = info.gid = 10001
                    info.mtime = int(time.time())
                    archive.addfile(info, io.BytesIO(data))
        temporary.chmod(0o600)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return SessionExport(output, expires_at)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export only the AgentRouter login from a Zen profile")
    parser.add_argument("--profile", type=Path, help="Zen profile directory; defaults to Windows discovery")
    parser.add_argument("--output", type=Path, default=Path("exports/zen-profile.tar.gz"))
    parser.add_argument("--base-url", default="https://agentrouter.org")
    args = parser.parse_args(argv)
    profile = args.profile or _discover_zen_profile()
    if profile is None:
        parser.error("Pass --profile or set ZEN_PROFILE_PATH to your logged-in Zen profile")
    try:
        result = export_session(profile, args.output, args.base_url)
    except (BrowserSessionError, OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(2, f"Session export failed: {exc}\n")
    print(f"Exported AgentRouter session: {result.archive}")
    print("Contains only one AgentRouter session cookie and its matching user ID.")
    if result.expires_at is not None:
        print(f"Stored cookie expiry: {result.expires_at:%Y-%m-%d %H:%M:%S UTC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
