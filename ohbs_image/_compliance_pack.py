from __future__ import annotations

import argparse
import html
import json
from importlib import resources
from pathlib import Path
from typing import Any

from ._config import _state_dir
from ._evidence_center import summarize_evidence
from ._registry import get_artifact

ASSESSMENT_SCHEMA = "https://ohbs-image.dev/compliance-assessment/v1"
PROFILES = ("mlps-2.0", "xinchuang-readiness")


def load_mapping(profile: str) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"unknown compliance profile: {profile}")
    raw = json.loads(resources.files("ohbs_image").joinpath(
        "compliance", f"{profile}.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("compliance mapping must be an object")
    value = {str(key): item for key, item in raw.items()}
    if value.get("schema") != "https://ohbs-image.dev/compliance-mapping/v1":
        raise ValueError("compliance mapping schema mismatch")
    return value


def _facts(artifact: dict[str, Any], operational: dict[str, Any] | None) -> dict[str, bool]:
    summary = summarize_evidence(artifact)
    facts = {str(check["name"]): bool(check["passed"]) for check in summary["checks"]}
    raw_evidence = artifact.get("evidence")
    evidence: dict[str, Any] = ({str(key): value for key, value in raw_evidence.items()}
                                if isinstance(raw_evidence, dict) else {})
    raw_labels = artifact.get("labels")
    labels: dict[str, Any] = ({str(key): value for key, value in raw_labels.items()}
                              if isinstance(raw_labels, dict) else {})
    profile = str(artifact.get("profile") or labels.get("profile") or "")
    facts.update({
        "image_identity_sanitized": bool(evidence.get("image_identity_sanitized")),
        "access_control_hardened": bool(evidence.get("access_control_hardened")),
        "audit_enabled": bool(evidence.get("audit_enabled")),
        "audit_evidence": bool(evidence.get("audit_evidence")),
        "network_boundary_hardened": bool(evidence.get("network_boundary_hardened")),
        "policy_decision": bool(artifact.get("policy_decision") or evidence.get("policy_decision")),
        "approval_audit": bool(artifact.get("approval_id") or evidence.get("approval_audit")),
        "domestic_os_profile": profile.startswith(("tencentos", "opencloudos", "kylin", "uos")),
        "offline_payload": bool(evidence.get("offline_payload", True)),
        "recovery_verified": bool((operational or {}).get("recovery_verified")),
    })
    return facts


def assess_compliance(mapping: dict[str, Any], artifact: dict[str, Any], *,
                      operational: dict[str, Any] | None = None) -> dict[str, Any]:
    facts = _facts(artifact, operational)
    controls = []
    for control in mapping["controls"]:
        required = [str(item) for item in control.get("evidence", [])]
        missing = [item for item in required if not facts.get(item, False)]
        status = "manual" if control.get("manual") else ("pass" if not missing else "gap")
        controls.append({**control, "status": status, "missing_evidence": missing})
    totals = {status: sum(item["status"] == status for item in controls)
              for status in ("pass", "gap", "manual")}
    return {"schema": ASSESSMENT_SCHEMA, "profile": mapping["profile"],
            "title": mapping["title"], "reference": mapping["reference"],
            "disclaimer": mapping["disclaimer"], "artifact_id": artifact.get("artifact_id"),
            "totals": totals, "controls": controls, "certification": False}


def render_assessment_html(result: dict[str, Any]) -> str:
    rows = "".join(f"<tr><td>{html.escape(str(row['id']))}</td><td>{html.escape(str(row['title']))}</td>"
                   f"<td class=\"{row['status']}\">{row['status'].upper()}</td>"
                   f"<td>{html.escape(', '.join(row['missing_evidence']))}</td></tr>"
                   for row in result["controls"])
    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\"><title>{html.escape(result['title'])}</title><style>body{{font:15px/1.5 system-ui;max-width:1100px;margin:auto;padding:32px;color:#17202b}}.warning{{border-left:5px solid #a55b13;padding:14px;background:#fff4df}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid #ddd;text-align:left}}.pass{{color:#18734d}}.gap{{color:#a23b3b}}.manual{{color:#946113}}@media print{{body{{padding:0}}}}</style></head><body><h1>{html.escape(result['title'])}</h1><p>Artifact: <strong>{html.escape(str(result['artifact_id']))}</strong></p><p class=\"warning\">{html.escape(result['disclaimer'])}</p><p>Pass {result['totals']['pass']} · Gap {result['totals']['gap']} · Manual {result['totals']['manual']}</p><table><thead><tr><th>Control</th><th>Title</th><th>Status</th><th>Missing evidence</th></tr></thead><tbody>{rows}</tbody></table></body></html>"""


def cmd_compliance_assess(args: argparse.Namespace) -> int:
    artifact = get_artifact(args.artifact_id, _state_dir() / "registry")
    if artifact is None:
        raise ValueError(f"artifact not found: {args.artifact_id}")
    result = assess_compliance(load_mapping(args.profile), artifact)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    base = output / f"{args.artifact_id}-{args.profile}"
    base.with_suffix(".json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    base.with_suffix(".html").write_text(render_assessment_html(result), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["totals"]["gap"] == 0 else 3
