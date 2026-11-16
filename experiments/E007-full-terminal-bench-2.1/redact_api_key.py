"""Redact leaked API keys from saved Harbor job files.

Before smith-nathanh/rho-agent#35, the agent's shell commands could see the
model key (``RHO_AGENT_API_KEY``), and a command that printed the environment
copied it into the trial's trace and trajectory. This script finds every key
value that follows a ``*_API_KEY`` variable name in the files under the given
directories, then replaces each occurrence of those values, wherever it
appears in those files, with ``REDACTED_API_KEY``.

It never prints a key: each one is shown as its first 7 characters, its length
and a short SHA-256 fingerprint. It reports by default and changes files only
with ``--write``.

Text files are edited as bytes. SQLite files (older runs kept a
``telemetry.db``) are edited through SQLite, since a shorter string written
into the raw file would corrupt it, and then vacuumed so no copy of the old
value is left in free pages.

Usage, from the main checkout (standard library only):
    python3 -I experiments/E007-full-terminal-bench-2.1/redact_api_key.py jobs
    python3 -I experiments/E007-full-terminal-bench-2.1/redact_api_key.py jobs --write
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

REPLACEMENT = "REDACTED_API_KEY"

# A *_API_KEY name, then = or : (inside JSON the quotes may be escaped), then
# the value. Matches env output (NAME=value), Python and JSON dicts
# ('NAME': 'value', "NAME": "value") and their escaped forms in a trace.
KEY_PATTERN = re.compile(
    rb"[A-Z][A-Z0-9_]*_API_KEY\\*[\"']?\s*[=:]\s*\\*[\"']?([A-Za-z0-9_\-]{20,})"
)


def is_key(value: bytes) -> bool:
    """Leave out placeholders and other variable names that follow a key name."""
    if value == REPLACEMENT.encode() or re.fullmatch(rb"[A-Z0-9_]+", value):
        return False
    return bool(re.search(rb"[a-z]", value) and re.search(rb"[0-9]", value))


def describe(key: bytes) -> str:
    digest = hashlib.sha256(key).hexdigest()[:12]
    return f"{key[:7].decode()}... ({len(key)} chars, sha256 {digest})"


def files_under(roots: list[Path]) -> list[Path]:
    found = []
    for root in roots:
        for dirpath, _, names in os.walk(root):
            found.extend(Path(dirpath) / name for name in names)
    return sorted(found)


def is_sqlite(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(16) == b"SQLite format 3\x00"


def redact_sqlite(path: Path, keys: set[bytes]) -> None:
    """Replace the keys in every column of every table, then rebuild the file."""
    conn = sqlite3.connect(path)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            for column in columns:
                for key in keys:
                    k = key.decode()
                    conn.execute(
                        f'UPDATE "{table}" SET "{column}" = replace("{column}", ?, ?) '
                        f'WHERE instr("{column}", ?) > 0',
                        (k, REPLACEMENT, k),
                    )
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    left = sum(path.read_bytes().count(key) for key in keys)
    if left:
        raise SystemExit(f"{path}: {left} occurrences remain after the SQLite update")


def write_atomically(path: Path, data: bytes) -> None:
    mode = path.stat().st_mode
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("roots", nargs="+", type=Path, help="directories to scan, e.g. jobs")
    ap.add_argument("--write", action="store_true", help="redact in place (default: report)")
    args = ap.parse_args()

    files = files_under(args.roots)

    # Pass 1: the key values, from wherever a *_API_KEY name precedes them.
    keys: set[bytes] = set()
    for path in files:
        data = path.read_bytes()
        keys.update(v for v in KEY_PATTERN.findall(data) if is_key(v))
    if not keys:
        print(f"No API keys found in {len(files)} files.")
        return 0
    print(f"Keys found in {len(files)} files:")
    for key in sorted(keys):
        print(f"  {describe(key)}")

    # Pass 2: every occurrence of those values, with or without the name.
    total, changed = 0, 0
    for path in files:
        data = path.read_bytes()
        count = sum(data.count(key) for key in keys)
        if not count:
            continue
        total += count
        changed += 1
        sqlite = is_sqlite(path)
        print(f"  {count:3} in {path}" + (" (SQLite)" if sqlite else ""))
        if args.write:
            if sqlite:
                redact_sqlite(path, keys)
            else:
                for key in keys:
                    data = data.replace(key, REPLACEMENT.encode())
                write_atomically(path, data)

    verb = "Replaced" if args.write else "Would replace"
    print(f"{verb} {total} occurrences in {changed} files with {REPLACEMENT}.")
    if not args.write:
        print("Dry run: nothing changed. Rerun with --write to redact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
