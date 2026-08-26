from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from ._channels import collect_channels
from ._config import _lineage_path
from ._registry import collect_artifacts
from ._slo import calculate_run_slo

METRICS_SCHEMA = "https://ohbs-image.dev/metrics-snapshot/v1"


def collect_metrics(root: Path, *, days: int = 30) -> dict[str, Any]:
    slo = calculate_run_slo(root, days=days)
    artifacts = collect_artifacts(root / "registry")
    artifact_status: dict[str, int] = {}
    replica_status: dict[str, int] = {}
    for artifact in artifacts:
        status = str(artifact.get("status") or "unknown")
        artifact_status[status] = artifact_status.get(status, 0) + 1
        replicas = artifact.get("replicas")
        if isinstance(replicas, dict):
            for replica in replicas.values():
                state = str(replica.get("status") or "unknown") if isinstance(replica, dict) else "unknown"
                replica_status[state] = replica_status.get(state, 0) + 1
    return {"schema": METRICS_SCHEMA, "window_days": slo["window_days"],
            "runs": {key: slo[key] for key in ("runs", "successful", "failed", "active", "retried")},
            "success_rate": slo["success_rate"], "retry_rate": slo["retry_rate"],
            "duration_seconds": slo["duration_seconds"],
            "failure_categories": slo["failure_categories"],
            "artifacts": artifact_status, "channels": len(collect_channels(root / "registry")),
            "replicas": replica_status}


def _sample(name: str, value: int | float | None, labels: dict[str, str] | None = None) -> str:
    label_text = ""
    if labels:
        escaped = [f'{key}="{val.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
                   for key, val in sorted(labels.items())]
        label_text = "{" + ",".join(escaped) + "}"
    return f"{name}{label_text} {0 if value is None else value}"


def prometheus_metrics(snapshot: dict[str, Any]) -> str:
    lines = ["# HELP ohbs_image_runs_total Runs observed in the selected window.",
             "# TYPE ohbs_image_runs_total gauge"]
    for outcome in ("successful", "failed", "active", "retried"):
        lines.append(_sample("ohbs_image_runs_total", snapshot["runs"][outcome],
                             {"outcome": outcome}))
    lines.extend(["# HELP ohbs_image_run_success_ratio Fraction of terminal runs that succeeded.",
                  "# TYPE ohbs_image_run_success_ratio gauge",
                  _sample("ohbs_image_run_success_ratio", None if snapshot["success_rate"] is None
                          else snapshot["success_rate"] / 100),
                  "# HELP ohbs_image_run_retry_ratio Fraction of runs that retried.",
                  "# TYPE ohbs_image_run_retry_ratio gauge",
                  _sample("ohbs_image_run_retry_ratio", None if snapshot["retry_rate"] is None
                          else snapshot["retry_rate"] / 100)])
    for quantile in ("p50", "p95", "max"):
        lines.append(_sample("ohbs_image_run_duration_seconds",
                             snapshot["duration_seconds"].get(quantile), {"quantile": quantile}))
    for category, count in snapshot["failure_categories"].items():
        lines.append(_sample("ohbs_image_run_failures_total", count, {"category": category}))
    for status, count in snapshot["artifacts"].items():
        lines.append(_sample("ohbs_image_artifacts", count, {"status": status}))
    lines.append(_sample("ohbs_image_channels", snapshot["channels"]))
    for status, count in snapshot["replicas"].items():
        lines.append(_sample("ohbs_image_replicas", count, {"status": status}))
    return "\n".join(lines) + "\n"


def otlp_metrics(snapshot: dict[str, Any]) -> dict[str, Any]:
    now = str(time.time_ns())
    metrics: list[dict[str, Any]] = []

    def point(name: str, value: int | float | None, attributes: dict[str, str] | None = None) -> None:
        metrics.append({"name": name, "gauge": {"dataPoints": [{"timeUnixNano": now,
            "asDouble": float(value or 0), "attributes": [{"key": key,
            "value": {"stringValue": val}} for key, val in sorted((attributes or {}).items())]}]}})

    for outcome in ("successful", "failed", "active", "retried"):
        point("ohbs.image.runs", snapshot["runs"][outcome], {"outcome": outcome})
    point("ohbs.image.run.success_ratio", None if snapshot["success_rate"] is None
          else snapshot["success_rate"] / 100)
    point("ohbs.image.run.retry_ratio", None if snapshot["retry_rate"] is None
          else snapshot["retry_rate"] / 100)
    for status, count in snapshot["artifacts"].items():
        point("ohbs.image.artifacts", count, {"status": status})
    point("ohbs.image.channels", snapshot["channels"])
    for status, count in snapshot["replicas"].items():
        point("ohbs.image.replicas", count, {"status": status})
    return {"resourceMetrics": [{"resource": {"attributes": [{"key": "service.name",
        "value": {"stringValue": "ohbs-image"}}]}, "scopeMetrics": [{"scope": {
        "name": "ohbs-image"}, "metrics": metrics}]}]}


def cmd_report_metrics(args: argparse.Namespace) -> int:
    snapshot = collect_metrics(_lineage_path().parent, days=args.days)
    if args.format == "prometheus":
        print(prometheus_metrics(snapshot), end="")
    elif args.format == "otlp-json":
        print(json.dumps(otlp_metrics(snapshot), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0
