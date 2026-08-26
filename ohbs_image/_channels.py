from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._logging import fail, ok
from ._registry import _artifact_path, _hash, _read_object, _root
from ._reports import _atomic_write_bytes, _state_lock

CHANNEL_SCHEMA = "https://ohbs-image.dev/channel/v1"
_SAFE_CHANNEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class ChannelConflictError(ValueError):
    """The channel changed after the caller last observed it."""


def _channel_path(bucket: str, channel: str, root: Path | None = None) -> Path:
    if not _SAFE_CHANNEL.fullmatch(bucket):
        raise ValueError(f"invalid bucket {bucket!r}")
    if not _SAFE_CHANNEL.fullmatch(channel):
        raise ValueError(f"invalid channel {channel!r}")
    return _root(root) / "channels" / bucket / f"{channel}.json"


def resolve_channel(bucket: str, channel: str,
                    root: Path | None = None) -> dict[str, Any]:
    pointer = _read_object(_channel_path(bucket, channel, root))
    if pointer is None:
        raise ValueError(f"channel {bucket}/{channel} not found")
    if pointer.get("schema") != CHANNEL_SCHEMA or pointer.get("document_hash") != _hash(pointer):
        raise ValueError(f"channel {bucket}/{channel} failed integrity verification")
    artifact_id = str(pointer.get("artifact_id") or "")
    artifact = _read_object(_artifact_path(artifact_id, root))
    if artifact is None:
        raise ValueError(f"artifact {artifact_id} not found")
    if artifact.get("document_hash") != _hash(artifact):
        raise ValueError(f"artifact {artifact_id} failed integrity verification")
    if artifact.get("bucket") != bucket:
        raise ValueError(f"artifact {artifact_id} does not belong to bucket {bucket}")
    if artifact.get("status") != "active":
        raise ValueError(f"artifact {artifact_id} is not active")
    return {"channel": pointer, "artifact": artifact}


def promote_channel(bucket: str, channel: str, artifact_id: str, *,
                    expected_generation: int | None = None,
                    actor: str = "unknown", reason: str = "",
                    root: Path | None = None) -> dict[str, Any]:
    path = _channel_path(bucket, channel, root)
    artifact = _read_object(_artifact_path(artifact_id, root))
    if artifact is None:
        raise ValueError(f"artifact {artifact_id} not found")
    if artifact.get("document_hash") != _hash(artifact):
        raise ValueError(f"artifact {artifact_id} failed integrity verification")
    if artifact.get("bucket") != bucket:
        raise ValueError(f"artifact {artifact_id} does not belong to bucket {bucket}")
    if artifact.get("status") != "active":
        raise ValueError(f"artifact {artifact_id} is not active")

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _state_lock(path)
    try:
        previous = _read_object(path)
        generation = int(previous.get("generation", 0)) if previous else 0
        if expected_generation is not None and expected_generation != generation:
            raise ChannelConflictError(
                f"channel {bucket}/{channel} generation is {generation}, "
                f"expected {expected_generation}")
        doc: dict[str, Any] = {
            "schema": CHANNEL_SCHEMA,
            "bucket": bucket,
            "channel": channel,
            "artifact_id": artifact_id,
            "generation": generation + 1,
            "previous_artifact_id": (previous or {}).get("artifact_id"),
            "promoted_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "promoted_by": actor,
            "reason": reason,
        }
        doc["document_hash"] = _hash(doc)
        _atomic_write_bytes(path, (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode())
        return doc
    finally:
        lock.rmdir()


def collect_channels(root: Path | None = None, bucket: str | None = None) -> list[dict[str, Any]]:
    base = _root(root) / "channels"
    paths = (base / bucket).glob("*.json") if bucket else base.glob("*/*.json")
    return [doc for path in sorted(paths) if (doc := _read_object(path)) is not None]


def cmd_channel_promote(args: argparse.Namespace) -> int:
    try:
        doc = promote_channel(args.bucket, args.channel, args.artifact_id,
                              expected_generation=args.expected_generation,
                              actor=args.actor, reason=args.reason)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 1
    if args.output == "json":
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        ok(f"Promoted {doc['artifact_id']} to {doc['bucket']}/{doc['channel']} "
           f"(generation {doc['generation']})")
    return 0


def cmd_channel_resolve(args: argparse.Namespace) -> int:
    try:
        result = resolve_channel(args.bucket, args.channel)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 1
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        pointer = result["channel"]
        print(f"{pointer['bucket']}/{pointer['channel']} -> {pointer['artifact_id']} "
              f"(generation {pointer['generation']})")
    return 0


def cmd_channel_list(args: argparse.Namespace) -> int:
    rows = collect_channels(bucket=args.bucket)
    if args.output == "json":
        print(json.dumps({"count": len(rows), "channels": rows}, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(f"{row.get('bucket')}/{row.get('channel')} -> {row.get('artifact_id')} "
                  f"(generation {row.get('generation')})")
    return 0
