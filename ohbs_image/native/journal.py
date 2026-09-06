"""Durable execution journal for restart-safe native builds."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

JOURNAL_SCHEMA = "https://ohbs-image.dev/native-journal/v1"
_JOURNAL_LOCK = threading.RLock()


def journal_path(workdir: Path) -> Path:
    return Path(workdir) / "native" / "journal.json"


def read_journal(workdir: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(journal_path(workdir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("schema") == JOURNAL_SCHEMA else None


def write_journal(workdir: Path, *, instance_id: str, phase: str,
                  status: str, provisioner: int | None = None,
                  detail: str = "", metadata: dict[str, Any] | None = None) -> None:
    with _JOURNAL_LOCK:
        target = journal_path(workdir)
        target.parent.mkdir(parents=True, exist_ok=True)
        doc = read_journal(workdir) or {}
        doc.update({
            "schema": JOURNAL_SCHEMA, "instance_id": instance_id,
            "phase": phase, "status": status, "updated_at_epoch": int(time.time()),
        })
        if metadata:
            doc.update(metadata)
        if provisioner is not None:
            doc["provisioner"] = provisioner
            if status == "completed":
                completed = doc.setdefault("completed_provisioners", [])
                if provisioner not in completed:
                    completed.append(provisioner)
        if detail:
            doc["detail"] = detail[:1000]
        _replace_journal(target, doc)


def heartbeat_journal(workdir: Path) -> bool:
    """Refresh liveness without changing the current phase or its status."""
    with _JOURNAL_LOCK:
        target = journal_path(workdir)
        doc = read_journal(workdir)
        if doc is None:
            return False
        now = int(time.time())
        doc["updated_at_epoch"] = now
        doc["heartbeat_at_epoch"] = now
        doc["heartbeat_sequence"] = int(doc.get("heartbeat_sequence") or 0) + 1
        _replace_journal(target, doc)
        return True


def _replace_journal(target: Path, doc: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix="journal-", suffix=".json", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def verify_resume_identity(journal: dict[str, Any], expected: dict[str, str]) -> list[str]:
    return [key for key, value in expected.items() if journal.get(key) != value]
