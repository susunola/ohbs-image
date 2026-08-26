from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ._channels import resolve_channel
from ._logging import fail
from ._policy import check_artifact
from ._registry import _hash

ADMISSION_SCHEMA = "https://ohbs-image.dev/consumer-admission/v1"


def resolve_admission(bucket: str, channel: str, *,
                      policy_path: Path | None = None,
                      environment: str | None = None,
                      root: Path | None = None) -> dict[str, Any]:
    resolved = resolve_channel(bucket, channel, root)
    pointer = resolved["channel"]
    artifact = resolved["artifact"]
    decision = (check_artifact(policy_path, str(artifact["artifact_id"]),
                               environment or channel, root)
                if policy_path is not None else None)
    denied = ([str(item.get("control")) for item in decision["checks"]
               if item.get("result") == "deny"] if decision else [])
    output: dict[str, Any] = {
        "schema": ADMISSION_SCHEMA,
        "allowed": not denied,
        "denied_controls": denied,
        "artifact": {
            "artifact_id": artifact.get("artifact_id"),
            "bucket": artifact.get("bucket"),
            "version": artifact.get("version"),
            "platform": artifact.get("platform"),
            "region": artifact.get("region"),
            "status": artifact.get("status"),
            "score": artifact.get("score"),
            "attestation_signed": artifact.get("attestation_signed"),
        },
        "channel": {
            "name": pointer.get("channel"),
            "generation": pointer.get("generation"),
            "promoted_at": pointer.get("promoted_at"),
        },
        "policy_decision": decision,
    }
    output["document_hash"] = _hash(output)
    return output


def terraform_external_result(admission: dict[str, Any]) -> dict[str, str]:
    artifact = admission["artifact"]
    channel = admission["channel"]
    decision = admission.get("policy_decision")
    return {
        "allowed": "true" if admission["allowed"] else "false",
        "image_id": str(artifact.get("artifact_id") or ""),
        "bucket": str(artifact.get("bucket") or ""),
        "version": str(artifact.get("version") or ""),
        "platform": str(artifact.get("platform") or ""),
        "region": str(artifact.get("region") or ""),
        "channel": str(channel.get("name") or ""),
        "generation": str(channel.get("generation") or ""),
        "decision_hash": str((decision or {}).get("document_hash") or ""),
        "admission_json": json.dumps(admission, ensure_ascii=False, sort_keys=True),
    }


def cmd_consumer_resolve(args: argparse.Namespace) -> int:
    try:
        admission = resolve_admission(
            args.bucket, args.channel,
            policy_path=Path(args.policy) if args.policy else None,
            environment=args.environment)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    if args.output == "terraform":
        print(json.dumps(terraform_external_result(admission), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(admission, ensure_ascii=False, indent=2))
    return 0 if admission["allowed"] else 1
