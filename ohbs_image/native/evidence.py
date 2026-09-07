"""Structured local evidence emitted by every native engine execution."""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .spec import BuildSpec

BUILD_RECORD_SCHEMA = "https://ohbs-image.dev/native-build-record/v1"


def _api_summary(requests: list[Any]) -> dict[str, Any]:
    """Summarize provider calls without dropping their per-request evidence."""
    valid = [item for item in requests if isinstance(item, dict)]
    durations = sorted(max(0.0, float(item.get("duration_ms") or 0.0))
                       for item in valid)
    attempts = [max(1, int(item.get("attempts") or 1)) for item in valid]
    p95_index = max(0, (95 * len(durations) + 99) // 100 - 1)
    slowest = sorted(
        valid,
        key=lambda item: (-float(item.get("duration_ms") or 0.0),
                          str(item.get("action") or ""),
                          str(item.get("region") or "")),
    )[:5]
    return {
        "calls": len(valid),
        "failed_calls": sum(item.get("status") == "failed" for item in valid),
        "total_attempts": sum(attempts),
        "retries": sum(value - 1 for value in attempts),
        "total_duration_ms": round(sum(durations), 3),
        "p95_duration_ms": round(durations[p95_index], 3) if durations else 0.0,
        "max_duration_ms": round(durations[-1], 3) if durations else 0.0,
        "slowest_calls": [{
            key: item[key] for key in (
                "action", "region", "request_id", "duration_ms",
                "attempts", "status", "error_type") if key in item
        } for item in slowest],
    }


def write_build_record(
    workdir: Path,
    spec: BuildSpec,
    *,
    run_id: str,
    provider: str,
    communicator: str,
    instance_id: str,
    image_ids: list[str],
    status: str,
    failure_phase: str,
    phase_durations: dict[str, float],
    started_at_epoch: int,
    cleanup_errors: list[str] | None = None,
    provider_evidence: dict[str, Any] | None = None,
    provisioner_results: list[dict[str, Any]] | None = None,
    cleanup_remaining: list[dict[str, str]] | None = None,
    failure: dict[str, Any] | None = None,
    phase_budgets: dict[str, float] | None = None,
) -> Path:
    """Atomically write a secret-free, machine-verifiable execution record."""
    spec_doc = spec.as_dict()
    provider_facts = dict(provider_evidence or {})
    requests = provider_facts.get("api_requests")
    if isinstance(requests, list):
        provider_facts["api_summary"] = _api_summary(requests)
        provider_facts["api_requests"] = sorted(
            requests, key=lambda item: (
                str(item.get("action") or ""), str(item.get("region") or ""),
                str(item.get("request_id") or "")) if isinstance(item, dict)
            else ("", "", ""))
    record = {
        "schema": BUILD_RECORD_SCHEMA,
        "run_id": run_id,
        "status": status,
        "provider": provider,
        "communicator": communicator,
        "instance_id": instance_id,
        "image_ids": image_ids,
        "failure_phase": failure_phase if status != "completed" else "",
        "failure": failure or {},
        "cleanup": {
            "status": "failed" if cleanup_errors else "completed",
            "errors": cleanup_errors or [],
            "remaining_resources": cleanup_remaining or [],
        },
        "provider_evidence": provider_facts,
        "started_at_epoch": started_at_epoch,
        "finished_at_epoch": int(time.time()),
        "build_spec_sha256": spec.sha256(),
        "build_identity": {
            "profile": spec.profile,
            "os_tag": spec.os_tag,
            "benchmark": spec.benchmark,
            "catalog_sha256": spec.catalog_sha256,
        },
        "target": spec_doc["target"],
        "provisioners": [{
            "index": index,
            "kind": item.kind,
            "label": item.label,
            "content_sha256": item.content_sha256,
        } for index, item in enumerate(spec.provisioners, 1)],
        "provisioner_results": provisioner_results or [],
        "phase_duration_seconds": {
            key: round(value, 3) for key, value in sorted(phase_durations.items())
        },
        "phase_budget_seconds": {
            key: round(value, 3) for key, value in sorted((phase_budgets or {}).items())
        },
    }
    target = Path(workdir) / "native" / "build-record.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="build-record-", suffix=".json",
                                      dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
    return target
