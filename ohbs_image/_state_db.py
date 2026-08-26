from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from ._config import _state_dir
from ._logging import fail, ok, warn

STATE_DB_SCHEMA = "https://ohbs-image.dev/state-database/v1"
_SCHEMA_VERSION = 2


def _stamp(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class StateDatabase:
    """Transactional state store with durable WAL and atomic worker leases."""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS objects (
                    path TEXT PRIMARY KEY, content BLOB NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rebuild_requests (
                    request_id TEXT PRIMARY KEY, document TEXT NOT NULL,
                    status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT, lease_expires_at TEXT, next_attempt_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rebuild_eligible
                    ON rebuild_requests(status, next_attempt_at, lease_expires_at);
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY, bucket TEXT NOT NULL,
                    version TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, document TEXT NOT NULL,
                    document_hash TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_bucket
                    ON artifacts(bucket);
                CREATE INDEX IF NOT EXISTS idx_artifacts_status
                    ON artifacts(status);
                CREATE INDEX IF NOT EXISTS idx_artifacts_version
                    ON artifacts(version);
                CREATE INDEX IF NOT EXISTS idx_artifacts_created
                    ON artifacts(created_at DESC);
            """)
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)",
                (str(_SCHEMA_VERSION),))

    def upsert_artifact(self, document: dict[str, Any]) -> None:
        """Atomically store the canonical artifact document."""
        artifact_id = str(document.get("artifact_id") or "")
        document_hash = str(document.get("document_hash") or "")
        if not artifact_id or not document_hash:
            raise ValueError("artifact_id and document_hash are required")
        self.initialize()
        serialized = json.dumps(document, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"))
        with self.transaction() as connection:
            connection.execute("""
                INSERT INTO artifacts
                  (artifact_id,bucket,version,status,created_at,document,document_hash,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                  bucket=excluded.bucket, version=excluded.version,
                  status=excluded.status, created_at=excluded.created_at,
                  document=excluded.document, document_hash=excluded.document_hash,
                  updated_at=excluded.updated_at
            """, (artifact_id, str(document.get("bucket") or "unknown"),
                  str(document.get("version") or artifact_id),
                  str(document.get("status") or "active"),
                  str(document.get("created_at") or ""), serialized,
                  document_hash, _stamp()))

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT document FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["document"]))
        return value if isinstance(value, dict) else None

    def search_artifacts(self, *, bucket: str = "", status: str = "",
                         version: str = "", query: str = "", label: str = "",
                         limit: int = 100, offset: int = 0) -> tuple[int, list[dict[str, Any]]]:
        self.initialize()
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, value in (("bucket", bucket), ("status", status), ("version", version)):
            if value:
                clauses.append(f"{column}=?")
                parameters.append(value)
        if query:
            clauses.append("(artifact_id LIKE ? OR bucket LIKE ? OR version LIKE ? OR document LIKE ?)")
            pattern = f"%{query}%"
            parameters.extend([pattern] * 4)
        if label:
            key, separator, value = label.partition("=")
            if not separator or not key or not value:
                raise ValueError("label must use key=value syntax")
            # JSON is canonical and compact, so this matches an exact label pair.
            clauses.append("document LIKE ?")
            parameters.append(f'%"{key}":"{value}"%')
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            count = int(connection.execute(
                f"SELECT COUNT(*) FROM artifacts{where}", parameters).fetchone()[0])
            rows = connection.execute(
                f"SELECT document FROM artifacts{where} "
                "ORDER BY created_at DESC, artifact_id LIMIT ? OFFSET ?",
                [*parameters, max(1, min(limit, 1000)), max(0, offset)]).fetchall()
        documents = [json.loads(str(row["document"])) for row in rows]
        return count, [row for row in documents if isinstance(row, dict)]

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def import_tree(self, root: Path) -> dict[str, int]:
        self.initialize()
        files = [p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".lock")]
        imported = queues = 0
        with self.transaction() as connection:
            for path in sorted(files):
                if path.resolve() == self.path:
                    continue
                relative = path.relative_to(root).as_posix()
                data = path.read_bytes()
                connection.execute(
                    "INSERT OR REPLACE INTO objects VALUES(?,?,?,?,?)",
                    (relative, data, _sha(data), len(data), _stamp()))
                imported += 1
                if relative.startswith("registry/rebuild_requests/") and path.suffix == ".json":
                    try:
                        document = json.loads(data)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(document, dict) and document.get("request_id"):
                        self._upsert_request(connection, document)
                        queues += 1
        return {"objects": imported, "rebuild_requests": queues}

    @staticmethod
    def _upsert_request(connection: sqlite3.Connection, document: dict[str, Any]) -> None:
        connection.execute("""
            INSERT OR REPLACE INTO rebuild_requests
            (request_id,document,status,attempt,worker_id,lease_expires_at,next_attempt_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
        """, (str(document["request_id"]), json.dumps(document, ensure_ascii=False),
              str(document.get("status", "queued")), int(document.get("attempt", 0)),
              document.get("worker_id"), document.get("lease_expires_at"),
              document.get("next_attempt_at"), _stamp()))

    def verify(self) -> dict[str, Any]:
        self.initialize()
        failures: list[str] = []
        with self.connect() as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                failures.append(integrity)
            rows = connection.execute("SELECT path,content,sha256,size FROM objects").fetchall()
            for row in rows:
                data = bytes(row["content"])
                if _sha(data) != row["sha256"] or len(data) != row["size"]:
                    failures.append(f"object hash mismatch: {row['path']}")
        return {"schema": STATE_DB_SCHEMA, "path": str(self.path), "integrity": integrity,
                "objects": len(rows), "valid": not failures, "failures": failures}

    def export_tree(self, destination: Path, *, force: bool = False) -> int:
        destination = destination.expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        count = 0
        with self.connect() as connection:
            for row in connection.execute("SELECT path,content,sha256 FROM objects ORDER BY path"):
                relative = PurePosixPath(str(row["path"]))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError(f"unsafe state object path: {relative}")
                target = destination.joinpath(*relative.parts)
                data = bytes(row["content"])
                if _sha(data) != row["sha256"]:
                    raise ValueError(f"object hash mismatch: {relative}")
                if target.exists() and not force:
                    raise FileExistsError(f"refusing to overwrite {target}; use --force")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = target.with_name(f".{target.name}.export.tmp")
                temporary.write_bytes(data)
                os.chmod(temporary, 0o600)
                os.replace(temporary, target)
                count += 1
        return count

    def backup(self, destination: Path) -> None:
        self.initialize()
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with self.connect() as source, sqlite3.connect(temporary) as target:
            source.backup(target)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)

    def claim(self, worker_id: str, *, lease_seconds: int = 900,
              now: datetime | None = None) -> dict[str, Any] | None:
        current = now or datetime.now(UTC)
        self.initialize()
        with self.transaction() as connection:
            row = connection.execute("""
                SELECT * FROM rebuild_requests WHERE
                  status='queued' OR
                  (status='retry_wait' AND (next_attempt_at IS NULL OR next_attempt_at<=?)) OR
                  (status='running' AND (lease_expires_at IS NULL OR lease_expires_at<=?))
                ORDER BY updated_at, request_id LIMIT 1
            """, (_stamp(current), _stamp(current))).fetchone()
            if row is None:
                return None
            document = json.loads(row["document"])
            if not isinstance(document, dict):
                raise ValueError(f"invalid rebuild request document: {row['request_id']}")
            attempt = int(row["attempt"]) + 1
            history = list(document.get("worker_history") or [])
            history.append({"status": "running", "at": _stamp(current),
                            "worker_id": worker_id, "attempt": attempt})
            document.update(status="running", attempt=attempt, worker_id=worker_id,
                            claimed_at=_stamp(current), worker_history=history,
                            lease_expires_at=_stamp(current + timedelta(seconds=lease_seconds)))
            self._upsert_request(connection, document)
            return dict(document)

    def finish(self, request: dict[str, Any], document: dict[str, Any]) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT worker_id FROM rebuild_requests WHERE request_id=?",
                (request["request_id"],)).fetchone()
            if row is None or row["worker_id"] != request.get("worker_id"):
                raise ValueError("rebuild request lease ownership changed")
            self._upsert_request(connection, document)


def _database(args: argparse.Namespace) -> StateDatabase:
    path = Path(args.database) if getattr(args, "database", "") else _state_dir() / "state.db"
    return StateDatabase(path)


def cmd_state_db_init(args: argparse.Namespace) -> int:
    db = _database(args)
    db.initialize()
    ok(f"Initialized transactional state database: {db.path}")
    return 0


def cmd_state_db_migrate(args: argparse.Namespace) -> int:
    db = _database(args)
    root = Path(args.source).expanduser().resolve() if args.source else _state_dir().resolve()
    files = sum(1 for p in root.rglob("*") if p.is_file() and p.resolve() != db.path)
    if not args.apply:
        warn(f"Dry run: {files} file(s) eligible; add --apply")
        return 0
    result = db.import_tree(root)
    ok(f"Imported {result['objects']} object(s), {result['rebuild_requests']} queue request(s)")
    return 0


def cmd_state_db_verify(args: argparse.Namespace) -> int:
    result = _database(args).verify()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


def cmd_state_db_export(args: argparse.Namespace) -> int:
    try:
        count = _database(args).export_tree(Path(args.destination), force=args.force)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    ok(f"Exported {count} object(s) to {Path(args.destination).expanduser().resolve()}")
    return 0


def cmd_state_db_backup(args: argparse.Namespace) -> int:
    _database(args).backup(Path(args.destination))
    ok(f"Created consistent backup: {Path(args.destination).expanduser().resolve()}")
    return 0
