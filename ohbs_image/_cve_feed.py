from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._logging import fail, ok, warn
from ._rebuild_events import EVENT_SCHEMA, process_rebuild_event
from ._registry import _hash
from ._reports import _atomic_write_bytes

OSV_QUERY_BATCH = "https://api.osv.dev/v1/querybatch"
CVE_FEED_SCHEMA = "https://ohbs-image.dev/cve-feed-state/v1"
Fetch = Callable[[str, dict[str, Any], int], dict[str, Any]]
_BATCH_SIZE = 1000


def _fetch_json(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "ohbs-image/cve-feed"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise ValueError(f"OSV feed request failed: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("OSV feed returned a non-object response")
    return value


def _inventory(path: Path) -> list[dict[str, str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid CVE inventory {path}") from exc
    rows = value.get("artifacts") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise ValueError("CVE inventory requires an artifacts array")
    queries: list[dict[str, str]] = []
    for artifact in rows:
        if not isinstance(artifact, dict) or not artifact.get("artifact_id"):
            raise ValueError("each inventory artifact requires artifact_id")
        packages = artifact.get("packages")
        if not isinstance(packages, list):
            raise ValueError("each inventory artifact requires packages")
        for package in packages:
            if not isinstance(package, dict) or not package.get("purl"):
                raise ValueError("each inventory package requires a purl")
            queries.append({"artifact_id": str(artifact["artifact_id"]),
                            "purl": str(package["purl"])})
    return queries


def _query_batches(packages: list[dict[str, str]], endpoint: str, timeout: int,
                   fetch: Fetch) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for start in range(0, len(packages), _BATCH_SIZE):
        chunk = packages[start:start + _BATCH_SIZE]
        queries = [{"package": {"purl": row["purl"]}} for row in chunk]
        response = fetch(endpoint, {"queries": queries}, timeout)
        results = response.get("results")
        if not isinstance(results, list) or len(results) != len(chunk):
            raise ValueError("OSV querybatch response does not match inventory")
        normalized = [dict(row) if isinstance(row, dict) else {} for row in results]
        pending = [(index, row.get("next_page_token"))
                   for index, row in enumerate(normalized) if row.get("next_page_token")]
        page_number = 0
        while pending:
            page_number += 1
            if page_number > 100:
                raise ValueError("OSV pagination exceeded safety limit")
            page_queries = [{"package": {"purl": chunk[index]["purl"]},
                             "page_token": token} for index, token in pending]
            page = fetch(endpoint, {"queries": page_queries}, timeout)
            page_results = page.get("results")
            if not isinstance(page_results, list) or len(page_results) != len(pending):
                raise ValueError("OSV paginated response does not match request")
            next_pending: list[tuple[int, Any]] = []
            for (index, _token), page_row in zip(pending, page_results, strict=True):
                if not isinstance(page_row, dict):
                    raise ValueError("OSV paginated result must be an object")
                existing = normalized[index].get("vulns")
                added = page_row.get("vulns")
                normalized[index]["vulns"] = [
                    *(existing if isinstance(existing, list) else []),
                    *(added if isinstance(added, list) else [])]
                if page_row.get("next_page_token"):
                    next_pending.append((index, page_row["next_page_token"]))
            pending = next_pending
        combined.extend(normalized)
    return combined


def sync_osv(inventory: Path, state_path: Path, *, apply: bool = False,
             root: Path | None = None, endpoint: str = OSV_QUERY_BATCH,
             timeout: int = 30, fetch: Fetch = _fetch_json) -> dict[str, Any]:
    packages = _inventory(inventory)
    results = _query_batches(packages, endpoint, timeout, fetch)
    previous: dict[str, Any] = {}
    if state_path.is_file():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            previous = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid CVE feed state {state_path}") from exc
        if previous.get("document_hash") != _hash(previous):
            raise ValueError(f"CVE feed state failed integrity verification: {state_path}")
    seen = {str(item) for item in previous.get("seen", [])}
    detected: list[dict[str, Any]] = []
    processed: list[dict[str, Any]] = []
    next_seen = set(seen)
    for package, result in zip(packages, results, strict=True):
        vulns = result.get("vulns", []) if isinstance(result, dict) else []
        for vulnerability in vulns if isinstance(vulns, list) else []:
            if not isinstance(vulnerability, dict) or not vulnerability.get("id"):
                continue
            vulnerability_id = str(vulnerability["id"])
            key = f"{package['artifact_id']}:{package['purl']}:{vulnerability_id}"
            next_seen.add(key)
            if key in seen:
                continue
            event = {"schema": EVENT_SCHEMA, "event_id": "osv:" + _hash({"key": key})[:32],
                     "type": "cve.detected", "artifact_id": package["artifact_id"],
                     "cve_id": vulnerability_id, "package_purl": package["purl"],
                     "occurred_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "source": endpoint}
            detected.append(event)
            if apply:
                processed.append(process_rebuild_event(event, apply=True,
                                                       actor="osv-feed", root=root))
    state: dict[str, Any] = {"schema": CVE_FEED_SCHEMA, "endpoint": endpoint,
        "inventory_hash": _hash({"packages": packages}), "seen": sorted(next_seen),
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    state["document_hash"] = _hash(state)
    if apply:
        _atomic_write_bytes(state_path,
                            (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode())
    return {"mode": "apply" if apply else "dry-run", "queried": len(packages),
            "detected": detected, "processed": processed, "new_count": len(detected)}


def cmd_cve_sync(args: argparse.Namespace) -> int:
    try:
        result = sync_osv(Path(args.inventory), Path(args.state), apply=args.apply,
                          endpoint=args.endpoint, timeout=args.timeout)
    except ValueError as exc:
        fail(str(exc))
        return 2
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.apply:
        ok(f"Processed {result['new_count']} new vulnerability event(s)")
    else:
        warn(f"Dry run: {result['new_count']} new vulnerability event(s); add --apply")
    return 0
