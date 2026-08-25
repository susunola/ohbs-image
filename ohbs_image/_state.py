from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from ._config import _state_dir
from ._logging import fail, info, ok
from ._reports import _state_lock

STATE_STATUS_SCHEMA = "https://ohbs-image.dev/state-status/v1"
STATE_PRUNE_SCHEMA = "https://ohbs-image.dev/state-prune/v1"

# Evidence subdirectories created by `state init` under the state root.
_STATE_SUBDIRS = ("plans", "runs", "releases", "provenance", "reports")

# Every `state status` bucket, in stable output order.
_STATUS_BUCKETS = ("lineage", "runs", "plans", "releases", "provenance", "reports")


class StateBackend(Protocol):
    def push(self, source: Path) -> None: ...
    def pull(self, destination: Path) -> None: ...


def _copy_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = destination / ".ohbs-sync.lock"
    deadline = time.monotonic() + 10
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise OSError(f"timed out waiting for state sync lock {lock}") from None
            time.sleep(0.05)
    if not source.exists():
        lock.rmdir()
        return
    try:
        for item in source.rglob("*"):
            relative = item.relative_to(source)
            if item.name.endswith(".lock") or ".lock." in item.name:
                continue
            target = destination / relative
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            elif item.is_file():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temp = target.with_name(f".{target.name}.sync.tmp")
                shutil.copy2(item, temp)
                os.chmod(temp, 0o600)
                os.replace(temp, target)
    finally:
        with suppress(OSError):
            lock.rmdir()


class LocalStateBackend:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def push(self, source: Path) -> None:
        if self.root == source.resolve() or source.resolve() in self.root.parents:
            raise OSError("local backend must not be the state directory or one of its children")
        _copy_tree(source, self.root)

    def pull(self, destination: Path) -> None:
        _copy_tree(self.root, destination)


class CosStateBackend:
    """Tencent COS backend through the official coscli binary.

    Credentials stay in coscli's supported environment/config mechanisms;
    ohbs-image never places secrets on the command line.
    """
    def __init__(self, uri: str):
        if not uri.startswith("cos://") or ".." in uri:
            raise ValueError("COS URI must be cos://bucket/prefix without '..'")
        self.uri = uri.rstrip("/") + "/"
        if not shutil.which("coscli"):
            raise OSError("coscli not found; install the official Tencent Cloud COS CLI")

    def _sync(self, source: str, destination: str) -> None:
        command = ["coscli", "sync", "--recursive"]
        config_path = os.environ.get("OHBS_IMAGE_COSCLI_CONFIG", "").strip()
        if config_path:
            command += ["-c", str(Path(config_path).expanduser())]
        command += [source, destination]
        result = subprocess.run(command,
                                timeout=3600)
        if result.returncode != 0:
            raise OSError(f"coscli sync failed with exit code {result.returncode}")

    def push(self, source: Path) -> None:
        self._sync(str(source.resolve()) + "/", self.uri)

    def pull(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._sync(self.uri, str(destination.resolve()) + "/")


def _backend(kind: str, location: str) -> StateBackend:
    if kind == "local":
        return LocalStateBackend(Path(location))
    return CosStateBackend(location)


# ------------------------------------------------------------- inspection

def _evidence_counts(root: Path) -> dict[str, int]:
    """Count evidence by bucket; absent roots report zeroes."""
    counts = dict.fromkeys(_STATUS_BUCKETS, 0)
    lineage = root / "lineage.jsonl"
    if lineage.is_file():
        with suppress(OSError):
            counts["lineage"] = sum(
                1 for line in lineage.read_text(encoding="utf-8").splitlines()
                if line.strip())
    patterns = {"runs": "*.json", "plans": "*-plan.json",
                "releases": "*.json", "provenance": "*.provenance.json",
                "reports": "*"}
    for name, pattern in patterns.items():
        directory = root / name
        if directory.is_dir():
            with suppress(OSError):
                counts[name] = sum(
                    1 for p in directory.iterdir()
                    if p.is_file() and p.match(pattern))
    return counts


def _dir_size(root: Path) -> int:
    total = 0
    for item in root.rglob("*"):
        if item.is_file():
            with suppress(OSError):
                total += item.stat().st_size
    return total


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _last_lineage_ts(root: Path) -> str:
    """Newest `ts` seen in lineage ("" when the file is absent or empty)."""
    path = root / "lineage.jsonl"
    if not path.is_file():
        return ""
    last = ""
    with suppress(OSError):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                ts = rec.get("ts")
                if isinstance(ts, str) and ts:
                    last = ts
    return last


def cmd_state_path(args: argparse.Namespace) -> int:
    """Print the absolute evidence root; script-friendly, no decorations."""
    print(_state_dir().expanduser().resolve())
    return 0


def cmd_state_status(args: argparse.Namespace) -> int:
    """Summarize the evidence root: layout, counts, usage (roadmap G)."""
    root = _state_dir().expanduser().resolve()
    doc: dict[str, Any] = {"schema": STATE_STATUS_SCHEMA,
                           "path": str(root), "exists": root.is_dir()}
    if root.is_dir():
        counts = _evidence_counts(root)
        doc["counts"] = counts
        doc["bytes"] = _dir_size(root)
        last = _last_lineage_ts(root)
        if last:
            doc["last_record"] = last
    if args.output == "json":
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    print(f"evidence root: {root}")
    if not root.is_dir():
        fail("state directory does not exist — run `state init`")
        return 0
    for name in _STATUS_BUCKETS:
        print(f"  {name:11s} {doc['counts'][name]}")
    print(f"  disk usage  {_human_bytes(doc['bytes'])}")
    if "last_record" in doc:
        print(f"  last record {doc['last_record']}")
    return 0


# ------------------------------------------------------------- lifecycle

def cmd_state_init(args: argparse.Namespace) -> int:
    """Idempotently create the evidence layout (roadmap G).

    Safe to run in CI and on every start; existing evidence is never
    touched, only missing directories and the lineage file are created.
    """
    root = _state_dir().expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lineage = root / "lineage.jsonl"
        if not lineage.exists():
            lineage.touch(mode=0o600)
        for name in _STATE_SUBDIRS:
            (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        fail(f"Could not initialize state: {exc}")
        return 1
    ok(f"State initialized: {root}")
    return 0


def _prune_evidence_files(root: Path, drop_ids: set[str]) -> list[str]:
    """Return the state-relative evidence files belonging to dropped runs.

    Only per-run evidence is eligible: runs/, plans/ and provenance/
    entries are keyed by run ID and die with their record.  releases/
    is the permanent approval audit trail and is NEVER pruned.
    """
    removed: list[str] = []
    for run_id in sorted(drop_ids):
        if not run_id:
            continue
        for candidate in (root / "runs" / f"{run_id}.json",
                          root / "plans" / f"{run_id}-plan.json"):
            if candidate.is_file():
                removed.append(str(candidate))
        provenance = root / "provenance"
        if provenance.is_dir():
            for prov in provenance.glob(f"*.{run_id}.provenance.json"):
                removed.append(str(prov))
                sig = Path(str(prov) + ".sig")
                if sig.is_file():
                    removed.append(str(sig))
    return sorted(removed)


def _record_is_old(rec: dict[str, Any], cutoff: datetime) -> bool:
    ts = str(rec.get("ts", ""))
    if not ts:
        return False
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) < cutoff
    except ValueError:
        return False  # unparseable timestamps are always kept


def cmd_state_prune(args: argparse.Namespace) -> int:
    """Retain recent lineage, drop superseded per-run evidence (roadmap G).

    Keeps the newest N records (--keep) and/or everything newer than
    --older-than DAYS; both flags compose.  The lineage file is rewritten
    atomically under a lock; release manifests are never touched.
    """
    root = _state_dir().expanduser().resolve()
    path = root / "lineage.jsonl"
    keep = max(int(getattr(args, "keep", 0) or 0), 0)
    older_than = max(int(getattr(args, "older_than", 0) or 0), 0)
    dry_run = bool(getattr(args, "dry_run", False))
    if not keep and not older_than:
        fail("nothing to prune: pass --keep N and/or --older-than DAYS")
        return 1

    rows: list[dict[str, Any]] = []
    if path.is_file():
        with suppress(OSError):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    kept = rows
    if older_than:
        cutoff = datetime.now(UTC) - timedelta(days=older_than)
        kept = [rec for rec in kept if not _record_is_old(rec, cutoff)]
    if keep and len(kept) > keep:
        kept = kept[-keep:]  # file order == append order (oldest first)
    kept_ids = {str(rec.get("run_id", "")) for rec in kept}
    drop_ids = {str(rec.get("run_id", ""))
                for rec in rows if str(rec.get("run_id", "")) not in kept_ids}
    removed_files = _prune_evidence_files(root, drop_ids)

    doc: dict[str, Any] = {
        "schema": STATE_PRUNE_SCHEMA,
        "dry_run": dry_run,
        "lineage_before": len(rows),
        "lineage_after": len(kept),
        "removed_runs": sorted(drop_ids),
        "removed_files": removed_files,
    }
    if args.output == "json":
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        if not drop_ids and not removed_files:
            print("Nothing to prune.")
        else:
            verb = "would remove" if dry_run else "removed"
            print(f"{verb} {len(drop_ids)} lineage record(s) and "
                  f"{len(removed_files)} evidence file(s)")
            for rel in removed_files:
                print(f"  {rel}")
    if dry_run:
        return 0

    if len(kept) != len(rows):
        try:
            lock = _state_lock(path)
            try:
                tmp = path.with_name(".lineage.prune.tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    os.chmod(tmp, 0o600)
                    for rec in kept:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            finally:
                lock.rmdir()
        except OSError as exc:
            fail(f"Could not rewrite lineage: {exc}")
            return 1
    for rel in removed_files:
        with suppress(OSError):
            Path(rel).unlink()
    return 0


# ------------------------------------------------------------- sync

def _diff_trees(source: Path, destination: Path) -> tuple[list[str], list[str], list[str]]:
    """Return (only_in_source, only_in_destination, changed) relative paths.

    Used by `state sync --check` to preview transfers without copying.
    Content equality is approximated by size + mtime (whole seconds).
    """
    def rel_map(base: Path) -> dict[str, Path]:
        if not base.is_dir():
            return {}
        return {str(p.relative_to(base)): p for p in base.rglob("*") if p.is_file()}

    src = rel_map(source)
    dst = rel_map(destination)
    common = sorted(set(src) & set(dst))
    changed: list[str] = []
    for rel in common:
        left, right = src[rel], dst[rel]
        try:
            if (left.stat().st_size != right.stat().st_size
                    or int(left.stat().st_mtime) != int(right.stat().st_mtime)):
                changed.append(rel)
        except OSError:
            changed.append(rel)
    return (sorted(set(src) - set(dst)),
            sorted(set(dst) - set(src)),
            changed)


def cmd_state_sync(args: argparse.Namespace) -> int:
    try:
        root = _state_dir().expanduser().resolve()
        if getattr(args, "check", False):
            if args.backend != "local":
                fail("--check is only supported for the local backend")
                return 1
            remote = Path(args.location).expanduser().resolve()
            source, destination = (root, remote) if args.direction == "push" else (remote, root)
            only, only_remote, changed = _diff_trees(source, destination)
            print(f"sync {args.direction}: {len(only)} to add, "
                  f"{len(changed)} to update, {len(only_remote)} not in source")
            for rel in only:
                print(f"  + {rel}")
            for rel in changed:
                print(f"  ~ {rel}")
            if not only and not changed:
                info("Nothing to transfer.")
            return 0
        backend = _backend(args.backend, args.location)
        if args.direction == "push":
            backend.push(root)
        else:
            backend.pull(root)
    except (OSError, ValueError) as exc:
        fail(f"State sync failed: {exc}")
        return 1
    ok(f"State {args.direction} complete: {args.backend}:{args.location}")
    info(f"Local evidence root: {root}")
    return 0
