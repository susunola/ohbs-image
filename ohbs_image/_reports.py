from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import ohbs_image

from ._config import ResolvedConfig
from ._logging import VERSION, info, ok, warn
from ._models import DeliveryReportView
from ._run_events import append_run_event, state_for_manifest


@dataclass
class _ReportContext:
    """Minimal render context for the delivery-report renderer.

    Attribute-compatible with :class:`ResolvedConfig` for everything the
    HTML renderer touches, so a stored lineage record can re-render a
    single-run compliance page (`report html`) without a rebuild or any
    cloud access. ``role_dir`` drives catalog/guidance lookup for the
    per-rule detail rows; it degrades to an empty catalog when a legacy
    record predates profile resolution.
    """

    profile_name: str
    level: int
    region: str
    zone: str
    source_image_id: str
    image_benchmark: str
    run_id: str
    role_dir: str
    attestation_required: bool = True


def _new_run_id() -> str:
    """Return a collision-resistant identifier shared by one build's evidence."""
    return str(uuid.uuid4())


def _state_lock(path: Path, timeout_s: float = 10.0) -> Path:
    """Acquire a portable directory lock, recovering an expired owner lease."""
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            lock.mkdir(mode=0o700)
            return lock
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
                if age > 300:
                    lock.rmdir()
                    warn(f"Recovered stale state lock {lock} ({age:.0f}s old)")
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise OSError(f"timed out waiting for state lock {lock}") from None
            time.sleep(0.05)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write evidence atomically and owner-readable only."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "xb") as fh:
            os.chmod(tmp, 0o600)
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with suppress(FileNotFoundError):
            tmp.unlink()


def _run_manifest_path(run_id: str) -> Path:
    """Return the fixed, state-root-contained manifest path for *run_id*."""
    if not re.fullmatch(r"[0-9a-f-]{36}", run_id):
        raise OSError("invalid run ID for state manifest")
    return ohbs_image._lineage_path().parent / "runs" / f"{run_id}.json"


def _release_manifest_path(image_id: str) -> Path:
    """Return a state-root-contained release manifest path for an image ID."""
    if not re.fullmatch(r"img-[A-Za-z0-9-]+", image_id):
        raise OSError("invalid image ID for release manifest")
    return ohbs_image._lineage_path().parent / "releases" / f"{image_id}.json"


def _read_release_manifest(image_id: str) -> dict[str, Any] | None:
    try:
        doc = json.loads(_release_manifest_path(image_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _evidence_reference(path: Path | None) -> str:
    """Return a portable state-root-relative evidence reference when possible."""
    if path is None:
        return ""
    root = ohbs_image._lineage_path().parent.resolve()
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return ""  # external paths are not portable release evidence


def _verify_release_manifest(image_id: str) -> list[str]:
    """Return integrity failures for a portable release manifest (empty = valid)."""
    doc = _read_release_manifest(image_id)
    if not doc:
        return [f"release manifest not found for {image_id}"]
    evidence = doc.get("evidence")
    if not isinstance(evidence, dict):
        return ["release manifest evidence is missing or malformed"]
    root = ohbs_image._lineage_path().parent.resolve()
    failures: list[str] = []
    for name in ("audit_report", "provenance", "html_report"):
        ref = evidence.get(name, "")
        digest = evidence.get(name.replace("report", "sha256") if name == "audit_report"
                              else name + "_sha256", "")
        # Keep the manifest readable even when an optional evidence artifact
        # was not produced, but never accept an absolute/path-traversal ref.
        if not ref and not digest:
            continue
        if not isinstance(ref, str) or not isinstance(digest, str) or not digest:
            failures.append(f"{name}: missing portable path or SHA-256")
            continue
        candidate = (root / ref).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            failures.append(f"{name}: path escapes evidence root")
            continue
        if not candidate.is_file():
            failures.append(f"{name}: evidence file is missing ({ref})")
        elif _file_hash(candidate) != digest:
            failures.append(f"{name}: SHA-256 mismatch")
    return failures


def _write_release_manifest(r: ResolvedConfig, image_ids: list[str], image_name: str,
                            score: float | None, report: Path | None,
                            provenance: Path | None, html_report: Path | None,
                            signed: bool) -> list[Path] | None:
    """Record an approved image as a promotable, evidence-bound release.

    This is cloud-agnostic metadata: promotion changes neither CVM permissions
    nor application deployment. External pipelines can consume the durable
    candidate → approved → promoted → rolled-back audit trail.
    """
    if not isinstance(r, ResolvedConfig) or not image_ids:
        return None
    paths: list[Path] = []
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for image_id in image_ids:
        try:
            path = _release_manifest_path(image_id)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = _state_lock(path)
            try:
                doc = {
                    "schema": "https://ohbs-image.dev/release-manifest/v1",
                    "image_id": image_id,
                    "image_name": image_name,
                    "state": "approved",
                    "approved_at": now,
                    "run_id": r.run_id,
                    "profile": r.profile_name,
                    "cis_level": r.level,
                    "region": r.region,
                    "source_image_id": r.source_image_id,
                    "score": score,
                    "attestation_signed": signed,
                    "evidence": {
                        "audit_report": _evidence_reference(report),
                        "audit_sha256": _file_hash(report) if report else "",
                        "provenance": _evidence_reference(provenance),
                        "provenance_sha256": _file_hash(provenance) if provenance else "",
                        "html_report": _evidence_reference(html_report),
                        "html_report_sha256": _file_hash(html_report) if html_report else "",
                    },
                    "promotions": [],
                }
                _atomic_write_bytes(path, (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
                paths.append(path)
            finally:
                lock.rmdir()
        except OSError as exc:
            warn(f"Could not write release manifest for {image_id}: {exc}")
            return None
    return paths


def _release_transition(image_id: str, environment: str, *, action: str,
                        actor: str, reason: str = "") -> Path | None:
    """Append a promotion or rollback transition to an approved release."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", environment):
        raise OSError("environment must use letters, digits, dot, dash, or underscore")
    if action not in {"promoted", "rolled_back"}:
        raise OSError("invalid release transition")
    path = _release_manifest_path(image_id)
    try:
        lock = _state_lock(path)
        try:
            doc = _read_release_manifest(image_id)
            if not doc:
                raise OSError(f"release manifest not found for {image_id}")
            promotions = doc.get("promotions")
            if not isinstance(promotions, list):
                promotions = []
            active = [item for item in promotions if isinstance(item, dict)
                      and item.get("environment") == environment and item.get("state") == "promoted"]
            if action == "rolled_back" and not active:
                raise OSError(f"{image_id} is not promoted to {environment}")
            promotions.append({"environment": environment, "state": action,
                               "actor": actor, "reason": reason,
                               "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})
            doc["promotions"] = promotions
            doc["state"] = "promoted" if action == "promoted" else "approved"
            doc["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            _atomic_write_bytes(path, (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            return path
        finally:
            lock.rmdir()
    except OSError as exc:
        warn(f"Could not {action} release {image_id}: {exc}")
        return None


def _read_run_manifest(run_id: str) -> dict[str, Any] | None:
    try:
        doc = json.loads(_run_manifest_path(run_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _write_run_manifest(r: ResolvedConfig, *, status: str, phase: str,
                        lease_hours: int = 48, resource: dict[str, str] | None = None,
                        notification: str | None = None,
                        next_action: str | None = None,
                        checkpoint: dict[str, Any] | None = None,
                        event_metadata: dict[str, Any] | None = None) -> Path | None:
    """Atomically update the recoverable lifecycle record for one run."""
    if not isinstance(r, ResolvedConfig) or not r.run_id:
        return None
    try:
        path = _run_manifest_path(r.run_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = _state_lock(path)
        try:
            current = _read_run_manifest(r.run_id) or {
                "schema": "https://ohbs-image.dev/run-manifest/v1",
                "run_id": r.run_id,
                "profile": r.profile_name,
                "region": r.region,
                "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "resources": [],
            }
            current["status"] = status
            current["phase"] = phase
            current["state"] = state_for_manifest(status, phase)
            current["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if status == "active":
                current["lease_expires_at"] = (datetime.now(UTC) + timedelta(hours=lease_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                current["lease_expires_at"] = current["updated_at"]
            if resource and resource not in current["resources"]:
                current["resources"].append(resource)
            if notification is not None:
                current["notification"] = notification
            if next_action is not None:
                current["next_action"] = next_action
            if checkpoint is not None:
                current["checkpoint"] = checkpoint
            _atomic_write_bytes(path, (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            try:
                event = append_run_event(
                    r.run_id, str(current["state"]), phase=phase,
                    reason=next_action or notification or "", metadata=event_metadata,
                    root=path.parent.parent)
                current["event_sequence"] = event["sequence"]
                current["event_hash"] = event["event_hash"]
                _atomic_write_bytes(
                    path, (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            except (OSError, ValueError) as exc:
                warn(f"Could not append run event for {r.run_id}: {exc}")
            return path
        finally:
            lock.rmdir()
    except OSError as exc:
        warn(f"Could not update run manifest for {r.run_id}: {exc}")
        return None


def _run_manifest_is_active(run_id: str) -> bool:
    """True only while an on-disk manifest has an unexpired active lease."""
    doc = _read_run_manifest(run_id)
    if not doc or doc.get("status") != "active":
        return False
    try:
        return datetime.fromisoformat(str(doc["lease_expires_at"]).replace("Z", "+00:00")) > datetime.now(UTC)
    except (KeyError, TypeError, ValueError):
        return False


def _missing_build_evidence(image_ids: list[str], score: float | None,
                            report: Path | None) -> list[str]:
    """Return the evidence missing from a successful Packer run."""
    missing: list[str] = []
    if not image_ids:
        missing.append("image ID")
    if score is None:
        missing.append("re-audit score")
    if report is None:
        missing.append("structured audit report")
    return missing


def _cis_rule_order_key(rule_id: object) -> tuple[int, ...]:
    """Sort dotted CIS identifiers numerically (1.2 before 1.10)."""
    text_id = str(rule_id)
    try:
        return tuple(int(part) for part in text_id.split("."))
    except ValueError:
        return (10**9,)


def _load_report_catalog(r: ResolvedConfig | _ReportContext) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Load the active catalog and optional CIS guidance for delivery output."""
    try:
        catalog_path = ohbs_image._catalog_path(r.role_dir, r.image_benchmark)
        raw_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog = [rule for rule in raw_catalog if isinstance(rule, dict)] if isinstance(raw_catalog, list) else []
        try:
            raw_guidance = json.loads((catalog_path.parent / "guidance.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw_guidance = {}
        if isinstance(raw_guidance, dict):
            guidance = {str(rule_id): entry for rule_id, entry in raw_guidance.items()
                        if isinstance(entry, dict)}
        elif isinstance(raw_guidance, list):
            guidance = {str(entry["id"]): entry for entry in raw_guidance
                        if isinstance(entry, dict) and isinstance(entry.get("id"), str)}
        else:
            guidance = {}
        return catalog, guidance
    except (OSError, json.JSONDecodeError):
        return [], {}


def _save_build_report(r: ResolvedConfig, image_name: str,
                       stdout_lines: list[str], workdir: Path) -> Path | None:
    """Archive the per-rule audit JSON on the BUILD machine (P-next).

    The in-image copy (/opt/ohbs-image-AUDIT-RESULT.json /
    C:\\ProgramData\\ohbs-image\\AUDIT-RESULT.json) travels with the image for
    drift/verify-image, but the operator's durable record belongs next to
    the lineage + provenance: ~/.ohbs-image/reports/<image-name>.<run-id>.json.

    Linux emits the file as a gzipped+base64 marker line in the packer log
    (the finalize provisioner); Windows fetches result.json back to
    <workdir>/ansible/reports/<host>/raw/ via the role's cis_report_json.
    Returns the saved path, or None when no report was found.
    """
    raw: bytes | None = None
    for line in stdout_lines:
        m = re.search(r"__CIS_IMAGE_AUDIT_B64__([A-Za-z0-9+/=]+)", line)
        if m:
            import base64 as _b64
            import gzip as _gz
            try:
                raw = _gz.decompress(_b64.b64decode(m.group(1)))
            except Exception:
                raw = None
            break
    if raw is None:
        # Windows path: result.json fetched to the controller by the role.
        cands = sorted(workdir.glob("ansible/reports/*/raw/result.json"))
        if cands:
            try:
                raw = cands[-1].read_bytes()
            except OSError:
                raw = None
    if not raw:
        return None
    try:
        json.loads(raw)  # don't archive garbage
    except ValueError:
        return None
    try:
        run_id = r.run_id if isinstance(r, ResolvedConfig) else ""
        suffix = f".{run_id}" if run_id else ""
        out = ohbs_image._reports_dir() / f"{image_name}{suffix}.json"
        _atomic_write_bytes(out, raw)
        return out
    except OSError:
        return None


def _write_build_html_report(r: ResolvedConfig | _ReportContext, image_ids: list[str], image_name: str,
                             score: float | None, audit_report: Path | None,
                             provenance: Path | None, signed: bool,
                             dest: Path | None = None) -> Path | None:
    """Write one portable, human-readable delivery report for an image build.

    *dest* overrides the default ``<state-dir>/reports/<image>.<run>.html``
    location — used by `scan --html` (arbitrary CI path) and `report html`.
    """
    if not isinstance(r, (ResolvedConfig, _ReportContext)):
        return None
    audit: dict[str, Any] = {}
    if audit_report:
        try:
            loaded = json.loads(audit_report.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                audit = loaded
        except (OSError, json.JSONDecodeError):
            pass
    summary = audit.get("summary", {}).get("all", {}) if isinstance(audit.get("summary"), dict) else {}
    catalog_rules, guidance_by_id = _load_report_catalog(r)
    def text(value: object) -> str:
        return html.escape(str(value if value not in (None, "") else "Not available"))

    def count(name: str) -> object:
        return summary.get(name, 0)

    score_s = f"{score:g}%" if isinstance(score, (int, float)) else "Not available"
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    status = "APPROVED" if signed or not r.attestation_required else "UNSIGNED"
    status_class = "approved" if status == "APPROVED" else "blocked"
    metrics = [("Manual", count("manual"), "warning"),
               ("Rule errors", count("error"), "danger"),
               ("Applied", count("applied"), "success"),
               ("Pending review", count("applied_pending"), "warning"),
               ("Fix failed", count("apply_failed"), "danger"),
               ("Skipped disruptive", count("skipped_disruptive"), "neutral")]
    metric_cards = "".join(
        f'<div class="card {tone}"><div class="label">{text(label)}</div><div class="value">{text(value)}</div></div>'
        for label, value, tone in metrics)
    findings: list[str] = []
    assessment_rows: list[str] = []
    assessment_details: list[str] = []
    results_by_id: dict[str, dict[str, Any]] = {}
    raw_results = audit.get("results", [])
    if isinstance(raw_results, list):
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            rule_status = str(item.get("status", ""))
            apply_status = str(item.get("apply_status", ""))
            rule_id = item.get("id", item.get("rule_id", "Not available"))
            title = item.get("title", item.get("name", ""))
            results_by_id[str(rule_id)] = item
            assessment_rows.append(
                f"<tr><td>{text(rule_id)}</td><td>{text(title)}</td>"
                f"<td>{text(rule_status or 'Not available')}</td>"
                f"<td>{text(apply_status or 'Not available')}</td></tr>")
            if rule_status not in ("fail", "manual", "error") and apply_status not in ("apply_failed", "applied_pending", "skipped_manual"):
                continue
            findings.append(f"<tr><td>{text(rule_id)}</td><td>{text(rule_status or 'Not available')}</td><td>{text(apply_status or 'Not available')}</td><td>{text(title)}</td></tr>")
            guidance = guidance_by_id.get(str(rule_id), {})
            detail_fields = [("Assessment", item.get("detail", rule_status or "Not available")),
                             ("Remediation", item.get("remediation") or guidance.get("remediation")
                              or item.get("apply_detail") or apply_status or "Not available"),
                             ("Rationale", item.get("rationale") or guidance.get("rationale", "Not available")),
                             ("Impact", item.get("impact") or guidance.get("impact", "Not available"))]
            detail_text = "".join(
                f"<dt>{text(label)}</dt><dd>{text(value)}</dd>"
                for label, value in detail_fields)
            assessment_details.append(
                f'<article class="rule-detail"><h3>{text(rule_id)} · {text(title)}</h3>'
                f"<dl>{detail_text}</dl></article>")
    # The engine emits every *selected* rule. The report, like CIS-CAT, is a
    # catalog document: retain every benchmark recommendation and make rules
    # outside a scoped run explicit instead of silently omitting them.
    display_rules = catalog_rules or list(results_by_id.values())
    assessment_rows.clear()
    evaluated_rules = 0
    not_evaluated_rules = 0
    for rule in sorted(display_rules, key=lambda value: _cis_rule_order_key(value.get("id", ""))):
        rule_id = str(rule.get("id", "Not available"))
        result = results_by_id.get(rule_id, {})
        rule_status = str(result.get("status", ""))
        assessment_type = rule.get("assessment", result.get("assessment", "Automated"))
        display_status = rule_status or ("manual" if assessment_type == "Manual" else "not evaluated (scope)")
        remediation = result.get("apply_status", "Not run")
        # A build runs exactly one CIS level. Rule applicability metadata may
        # mention multiple profiles, but showing it here makes one run look
        # like a mixed L1/L2 assessment. Report the selected run level only.
        run_level = f"L{r.level}"
        if rule_status in ("pass", "fail", "manual", "error"):
            evaluated_rules += 1
        else:
            not_evaluated_rules += 1
        status_token = re.sub(r"[^a-z0-9]+", "-", display_status.lower()).strip("-") or "unknown"
        assessment_rows.append(
            f"<tr data-status=\"{status_token}\"><td>{text(rule_id)}</td><td>{text(rule.get('title', result.get('title', '')))}</td>"
            f"<td>{text(run_level)}</td><td>{text(assessment_type)}</td>"
            f"<td>{text(display_status)}</td><td>{text(remediation)}</td></tr>")
    findings_html = ("<section class=\"findings\"><div class=\"section-heading\"><div><p>EXCEPTIONS</p><h2>Rules requiring attention</h2></div><strong>"
                     f"{len(findings)} record{'s' if len(findings) != 1 else ''}</strong></div><table><tr><th>Rule</th><th>Audit</th><th>Remediation</th><th>Title</th></tr>"
                     + "".join(findings[:200]) + "</table></section>") if findings else ""
    total_rules = len(assessment_rows)
    view = DeliveryReportView(
        total_rules=total_rules, evaluated_rules=evaluated_rules,
        not_evaluated_rules=not_evaluated_rules,
        coverage_percent=round(100 * evaluated_rules / total_rules) if total_rules else None)
    coverage_s = f"{view.coverage_percent}%" if view.coverage_percent is not None else "Not available"
    coverage_cards = (f'<div class="card neutral"><div class="label">Evaluated</div><div class="value">{view.evaluated_rules}</div></div>'
                      f'<div class="card neutral"><div class="label">Not evaluated</div><div class="value">{view.not_evaluated_rules}</div></div>'
                      f'<div class="card neutral"><div class="label">Catalog coverage</div><div class="value">{coverage_s}</div></div>')
    results_html = ("<section id=\"assessment-results\" class=\"results\"><div class=\"section-heading\"><div><p>ASSESSMENT RESULTS</p>"
                    "<h2>Recommendation results</h2></div><strong>"
                    f"{view.total_rules} recommendations · {view.evaluated_rules} evaluated ({coverage_s})</strong></div>"
                    "<div class=\"results-tools\"><label>Audit status <select id=\"audit-filter\"><option value=\"all\">All</option><option value=\"pass\">Pass</option><option value=\"fail\">Fail</option><option value=\"manual\">Manual</option><option value=\"error\">Error</option><option value=\"not-evaluated-scope\">Not evaluated (scope)</option></select></label><label>Search recommendation <input id=\"audit-search\" type=\"search\" placeholder=\"Rule ID or text\"></label><span id=\"audit-count\"></span></div>"
                    "<table id=\"assessment-table\"><tr><th>Rule</th><th>Recommendation</th><th>Run level</th><th>Assessment</th><th>Audit</th><th>Remediation</th></tr>"
                    + "".join(assessment_rows[:1000]) + "</table></section>") if assessment_rows else ""
    details_html = ("<section id=\"assessment-details\" class=\"assessment-details\"><div class=\"section-heading\"><div><p>ASSESSMENT DETAILS</p>"
                    "<h2>Exception review</h2></div><strong>"
                    f"{len(assessment_details)} item{'s' if len(assessment_details) != 1 else ''}</strong></div>"
                    + "".join(assessment_details[:200]) + "</section>") if assessment_details else ""
    rows = [("Profile", r.profile_name), ("CIS level", f"L{r.level}"),
            ("Region / zone", f"{r.region} / {r.zone}"), ("Source image", r.source_image_id),
            ("Output image IDs", ", ".join(image_ids) or "Not available"), ("Benchmark", r.image_benchmark),
            ("Run ID", r.run_id), ("Audit report", str(audit_report or "Not available")),
            ("Provenance", str(provenance or "Not available"))]
    detail_rows = "".join(f"<tr><th>{text(k)}</th><td>{text(v)}</td></tr>" for k, v in rows)
    html_doc = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ohbs-image delivery report · {text(image_name)}</title><style>:root{{--ink:#15212c;--muted:#627487;--line:#dce4eb;--bg:#f3f6f8;--navy:#173a63;--ok:#06734d;--bad:#a12e2b}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}}main{{max-width:1000px;margin:auto;padding:32px 20px 64px}}header{{background:var(--navy);color:white;padding:30px;border-radius:15px 15px 0 0}}h1{{margin:0;font-size:27px}}.sub{{color:#cfdef0;margin-top:7px}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}}.card,table{{background:white;border:1px solid var(--line);border-radius:10px}}.card{{padding:17px}}.label{{font-size:11px;font-weight:800;letter-spacing:.8px;color:var(--muted);text-transform:uppercase}}.value{{font-size:27px;font-weight:800;margin-top:5px}}.approved{{color:var(--ok)}}.blocked{{color:var(--bad)}}h2{{font-size:18px;margin:32px 0 12px}}table{{border-collapse:separate;border-spacing:0;width:100%;overflow:hidden}}th,td{{padding:12px 15px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;word-break:break-word}}th{{width:31%;color:var(--muted);font-size:13px}}tr:last-child th,tr:last-child td{{border-bottom:0}}footer{{color:var(--muted);font-size:12px;margin-top:22px}}@media(max-width:700px){{.grid{{grid-template-columns:repeat(2,1fr)}}header{{border-radius:10px}}}}</style><body><main><header><h1>Image delivery report</h1><div class="sub">{text(image_name)} · generated by ohbs-image</div></header><section class="grid"><div class="card"><div class="label">Release status</div><div class="value {status_class}">{status}</div></div><div class="card"><div class="label">Re-audit score</div><div class="value">{text(score_s)}</div></div><div class="card"><div class="label">Rules passed</div><div class="value">{text(summary.get("pass", "—"))}</div></div><div class="card"><div class="label">Rules failed</div><div class="value">{text(summary.get("fail", "—"))}</div></div></section><h2>Build identity</h2><table>{detail_rows}</table><h2>Evidence</h2><table><tr><th>Attestation</th><td>{"Signed" if signed else "Not signed"}</td></tr><tr><th>SBOM packages</th><td>{text(summary.get("sbom_packages", "—"))}</td></tr><tr><th>Audit mode</th><td>{text(audit.get("mode", "—"))}</td></tr></table><footer>This report is a human-readable view. Verify the referenced provenance signature and machine-readable result for release automation.</footer></main></body></html>'''
    html_doc = html_doc.replace("</section><h2>Build identity", metric_cards + coverage_cards + "</section><h2>Build identity")
    html_doc = html_doc.replace('<div class="card"><div class="label">Rules passed',
                                '<div class="card success"><div class="label">Pass')
    html_doc = html_doc.replace('<div class="card"><div class="label">Rules failed',
                                '<div class="card danger"><div class="label">Fail')
    html_doc = html_doc.replace("<header>", '<header><p class="dossier">Security release dossier</p>')
    html_doc = html_doc.replace("</header>",
                                f'<div class="release-stamp {status_class}"><span>Release decision</span><strong>{status}</strong></div><div class="cover-meta"><span>Assessment report</span><span>{text(generated_at)}</span><span>Run {text(r.run_id)}</span></div></header>')
    html_doc = html_doc.replace("</header><section class=\"grid\">",
                                '</header><nav aria-label="Report sections"><a href="#summary">Summary</a><a href="#profiles">Profiles</a><a href="#assessment-results">Assessment Results</a><a href="#assessment-details">Assessment Details</a><a href="#evidence">Evidence</a></nav><section id="summary" class="summary"><div class="grid">')
    html_doc = html_doc.replace("</section><h2>Build identity",
                                "</div></section><h2>Build identity")
    html_doc = html_doc.replace(
        "<h2>Build identity</h2><table>",
        '<section id="profiles" class="identity"><div class="section-heading"><div>'
        '<p>PROFILES</p><h2>Build target and profile</h2></div></div><table>')
    html_doc = html_doc.replace(
        "</table><h2>Evidence</h2><table>",
        '</table></section><section id="evidence" class="evidence">'
        '<div class="section-heading"><div><p>EVIDENCE</p><h2>Evidence</h2>'
        '</div></div><table>')
    html_doc = html_doc.replace("</table><footer>", "</table></section><footer>")
    # Insert the large assessment sections only after Profiles and Evidence
    # have been made into balanced sibling sections.  Inserting them before
    # the identity close used to make the whole report nest under Profiles.
    html_doc = html_doc.replace(
        '<section id="evidence"',
        results_html + findings_html + details_html + '<section id="evidence"',
        1)
    html_doc = html_doc.replace("</style>", '''
/* Release dossier visual system: static, high-contrast, and print-safe. */
:root{--ink:#102a43;--muted:#61758a;--line:#d9e2ec;--bg:#eef3f7;--navy:#0c2744;--teal:#087c73;--ok:#087850;--bad:#b33a3a;--warn:#a56613}
body{background:var(--bg);color:var(--ink);font:14px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1120px;padding:36px 24px 64px} header{position:relative;min-height:175px;padding:30px 32px;background:linear-gradient(122deg,#0c2744 0%,#123c5f 100%);border-radius:14px;box-shadow:0 18px 44px rgb(16 42 67/.14)}
h1{font-size:30px;letter-spacing:-.035em;line-height:1.1}.dossier,.section-heading p{margin:0 0 8px;font-size:11px;font-weight:800;letter-spacing:.13em;color:#8fc1ce;text-transform:uppercase}.sub{color:#d5e4f0;margin-top:10px}.release-stamp{position:absolute;right:30px;top:30px;min-width:170px;padding:13px 15px;border:1px solid rgb(255 255 255/.24);border-radius:10px;background:rgb(255 255 255/.08)}.release-stamp span{display:block;color:#c2d8e8;font-size:10px;letter-spacing:.12em;text-transform:uppercase;font-weight:800}.release-stamp strong{display:block;margin-top:3px;font-size:17px}.release-stamp.approved strong{color:#83efc5}.release-stamp.blocked strong{color:#ffb5ab}
nav{display:flex;gap:0;margin:0 1px;padding:0;background:#fff;border:1px solid var(--line);border-top:0;border-radius:0 0 12px 12px;overflow-x:auto}nav a{padding:11px 14px;color:#526a80;border-right:1px solid var(--line);font-size:12px;font-weight:750;text-decoration:none;white-space:nowrap}nav a:hover{color:var(--teal);background:#f7fafc}
.grid{grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0 28px}.card{min-height:104px;padding:15px;border-radius:10px;border:1px solid var(--line);border-top:3px solid #9fb3c8;box-shadow:0 2px 8px rgb(16 42 67/.035)}.label{font-size:11px;letter-spacing:.08em}.value{font-size:30px;letter-spacing:-.05em;line-height:1.1}.card.success{border-top-color:var(--ok)}.card.success .value{color:var(--ok)}.card.danger{border-top-color:#da5757}.card.danger .value{color:var(--bad)}.card.warning{border-top-color:#d4943b}.card.warning .value{color:var(--warn)}
.identity,.evidence,.findings,.results,.assessment-details{margin-top:18px;padding:23px 24px;background:#fff;border:1px solid var(--line);border-radius:14px;box-shadow:0 2px 8px rgb(16 42 67/.035)}.section-heading{display:flex;justify-content:space-between;gap:16px;align-items:end;margin-bottom:16px}.section-heading h2{margin:0;font-size:20px;letter-spacing:-.025em}.section-heading p{color:var(--muted)}.section-heading strong{color:var(--muted);font-size:12px;white-space:nowrap}h2{font-size:18px;margin:0 0 12px}table{border:1px solid var(--line);border-radius:9px;overflow:hidden}th,td{padding:11px 13px}th{background:#f7fafc;color:#526a80;font-size:11px;letter-spacing:.08em;text-transform:uppercase}.identity th{width:31%;background:#f9fbfc}.findings td:first-child,.results td:first-child{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}.rule-detail{border-top:1px solid var(--line);padding:17px 0}.rule-detail:first-of-type{border-top:0;padding-top:0}.rule-detail h3{margin:0 0 11px;font-size:14px}.rule-detail dl{display:grid;grid-template-columns:140px 1fr;gap:7px 15px;margin:0}.rule-detail dt{color:var(--muted);font-size:11px;font-weight:800;letter-spacing:.07em;text-transform:uppercase}.rule-detail dd{margin:0;word-break:break-word}footer{border-top:1px solid var(--line);padding-top:16px;margin-top:28px}
@media(max-width:700px){main{padding:18px 14px 40px}header{min-height:0;padding:24px}.release-stamp{position:static;margin-top:18px}.grid{grid-template-columns:repeat(2,1fr)}.identity,.evidence,.findings,.results,.assessment-details{padding:19px}.rule-detail dl{grid-template-columns:1fr}.rule-detail dt{margin-top:8px}}@media print{body{background:#fff;font-size:11px}main{max-width:none;padding:0}header{box-shadow:none}nav{display:none}.card,.identity,.evidence,.findings,.results,.assessment-details{box-shadow:none;break-inside:avoid}.grid{margin:10px 0 16px}}
</style>''')
    html_doc = html_doc.replace("</style>", '''
.results-tools{display:flex;align-items:end;gap:16px;flex-wrap:wrap;margin:0 0 15px;padding:13px;background:#f3f7f8;border:1px solid var(--line)}.results-tools label{display:grid;gap:4px;color:var(--muted);font-size:11px;font-weight:700}.results-tools select,.results-tools input{min-height:30px;padding:4px 7px;border:1px solid #9baeba;background:#fff;color:var(--ink);font:13px Arial,sans-serif}.results-tools input{width:230px}.results-tools span{margin-left:auto;color:var(--muted);font-size:12px}@media(max-width:700px){.results-tools{align-items:stretch}.results-tools input{width:100%}.results-tools span{margin-left:0}}@media print{.results-tools{display:none}}
</style>''')
    html_doc = html_doc.replace("</body>", '''<script>
(() => {
  const select = document.getElementById("audit-filter");
  const search = document.getElementById("audit-search");
  const table = document.getElementById("assessment-table");
  const count = document.getElementById("audit-count");
  if (!select || !search || !table || !count) return;
  const rows = Array.from(table.querySelectorAll("tr[data-status]"));
  const apply = () => {
    const status = select.value;
    const needle = search.value.trim().toLowerCase();
    let shown = 0;
    rows.forEach((row) => {
      const match = (status === "all" || row.dataset.status === status)
        && (!needle || (row.textContent || "").toLowerCase().includes(needle));
      row.hidden = !match;
      if (match) shown += 1;
    });
    count.textContent = `${shown} of ${rows.length} shown`;
  };
  select.addEventListener("change", apply);
  search.addEventListener("input", apply);
  apply();
})();
</script></body>''')
    html_doc = html_doc.replace("</style>", '''
/* Assessment-report treatment: data-led, restrained, and export-safe. */
:root{--ink:#1c2b36;--muted:#61727f;--line:#cfd8de;--bg:#f1f4f5;--navy:#17384f;--teal:#16756c;--ok:#287a4f;--bad:#b43d3d;--warn:#9a651e}
body{background:var(--bg);font:14px/1.5 Arial,"Helvetica Neue",sans-serif}main{max-width:1180px;padding:42px 28px 72px}header{min-height:242px;padding:35px 38px 66px;border-radius:0;border-top:7px solid var(--teal);background:var(--navy);box-shadow:none}h1{font-size:33px;font-weight:650;letter-spacing:-.025em}.dossier{font-size:12px;letter-spacing:.11em}.sub{max-width:660px;font-size:16px}.release-stamp{right:38px;top:38px;border-radius:0;border-color:#88a2b3;background:transparent}.cover-meta{position:absolute;bottom:19px;left:38px;right:38px;display:flex;gap:24px;padding-top:12px;border-top:1px solid rgb(255 255 255/.24);color:#c2d2dc;font-size:11px}.cover-meta span:first-child{font-weight:750;letter-spacing:.08em;text-transform:uppercase}nav{border-radius:0;border:0;border-bottom:1px solid var(--line);background:transparent}nav a{padding:15px 16px;border-right:0;color:#425a6b}nav a:first-child{padding-left:0}nav a:hover{color:var(--teal);background:transparent}.summary{margin:30px 0 0;background:#fff;border:1px solid var(--line);padding:26px 28px}.grid{grid-template-columns:repeat(5,1fr);gap:0;margin:0}.card{min-height:94px;padding:10px 15px;border:0;border-left:1px solid var(--line);border-top:0;border-radius:0;box-shadow:none}.card:first-child{border-left:0;padding-left:0}.card:nth-child(1),.card:nth-child(2){grid-column:span 1}.card:nth-child(n+6){margin-top:18px;padding-top:16px;border-top:1px solid var(--line)}.label{font-size:10px;color:var(--muted);letter-spacing:.1em}.value{font-size:28px;font-weight:650}.card.success,.card.danger,.card.warning{border-top:0}.card.success .value{color:var(--ok)}.card.danger .value{color:var(--bad)}.card.warning .value{color:var(--warn)}.identity,.evidence,.findings,.results,.assessment-details,.recommendation-summary{margin-top:28px;padding:28px;background:#fff;border:1px solid var(--line);border-radius:0;box-shadow:none}.section-heading{align-items:baseline;margin-bottom:18px}.section-heading h2{font-size:20px;font-weight:650}.section-heading p{font-size:10px;letter-spacing:.12em}.section-heading strong{font-weight:600}table{border:0;border-radius:0}th,td{padding:10px 12px;border-bottom:1px solid #e2e7ea}th{background:#eaf0f2;color:#355063;font-size:10px;font-weight:750}.identity th{background:#f6f8f9}.recommendation-summary td:nth-child(2){font-weight:700;color:var(--teal)}.results tr:nth-child(even) td,.findings tr:nth-child(even) td{background:#fafcfc}.rule-detail{padding:20px 0}.rule-detail h3{font-size:15px;font-weight:650}.rule-detail dl{grid-template-columns:130px 1fr}.rule-detail dt{font-size:10px}.evidence tr:first-child td{font-weight:650}.identity,.evidence{break-inside:avoid}footer{font-size:11px;color:var(--muted)}@media(max-width:700px){main{padding:20px 14px 40px}header{min-height:0;padding:27px 22px 60px}.release-stamp{position:static;margin-top:24px}.cover-meta{left:22px;right:22px;bottom:17px;gap:8px;flex-direction:column}.summary,.identity,.evidence,.findings,.results,.assessment-details,.recommendation-summary{padding:20px}.grid{grid-template-columns:repeat(2,1fr)}.card:nth-child(odd){border-left:0;padding-left:0}.card:nth-child(n+3){margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}}@media print{body{background:#fff}main{padding:0}header{min-height:180px}.summary,.identity,.evidence,.findings,.results,.assessment-details,.recommendation-summary{border-color:#aebbc4}.cover-meta{bottom:12px}.grid{grid-template-columns:repeat(5,1fr)}}
/* Component isolation: wide audit tables must not inherit identity-table widths. */
nav a{color:#a9bcc9}.summary .grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));height:auto}.summary .card{display:block;min-width:0;overflow:visible}.summary .value{display:block;visibility:visible;opacity:1;color:var(--ink)}.summary .card.success .value{color:var(--ok)}.summary .card.danger .value{color:var(--bad)}.summary .card.warning .value{color:var(--warn)}.summary .approved{color:var(--ok)}.recommendation-summary,.results,.findings{overflow-x:auto}.recommendation-summary table,.results table,.findings table{min-width:760px;table-layout:auto}.recommendation-summary th,.results th,.findings th{width:auto;white-space:nowrap}.identity table,.evidence table{table-layout:fixed}.identity th,.evidence th{width:220px}.identity td,.evidence td{overflow-wrap:anywhere}
#assessment-table{min-width:1000px;table-layout:fixed}#assessment-table th:nth-child(1){width:9%}#assessment-table th:nth-child(2){width:40%}#assessment-table th:nth-child(3){width:8%}#assessment-table th:nth-child(4){width:12%}#assessment-table th:nth-child(5){width:17%}#assessment-table th:nth-child(6){width:14%}#assessment-table td{overflow-wrap:anywhere}.recommendation-summary table{min-width:900px}
</style>''')
    if str(audit.get("mode", "")).lower() == "demo":
        html_doc = html_doc.replace("</style>", '''
.demo-warning{position:sticky;top:0;z-index:20;padding:12px 18px;background:#8b1e1e;color:#fff;text-align:center;font-weight:850;letter-spacing:.08em;text-transform:uppercase}.demo-warning small{display:block;margin-top:2px;font-weight:600;letter-spacing:0;text-transform:none}.demo-watermark{position:fixed;inset:42% auto auto 50%;z-index:10;transform:translate(-50%,-50%) rotate(-24deg);color:rgb(139 30 30/.12);font-size:clamp(44px,9vw,112px);font-weight:900;letter-spacing:.08em;pointer-events:none;white-space:nowrap}@media print{.demo-warning{position:static}.demo-watermark{color:rgb(139 30 30/.16)}}
</style>''')
        html_doc = html_doc.replace(
            "<body>",
            '<body><div class="demo-warning">Demo data — not audit evidence'
            '<small>Synthetic results for product evaluation only; do not use for compliance.'
            '</small></div><div class="demo-watermark" aria-hidden="true">DEMO ONLY</div>')
    try:
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", image_name) or "image"
        path = dest if dest is not None else (
            ohbs_image._reports_dir() / f"{safe_name}.{r.run_id}.html")
        _atomic_write_bytes(path, html_doc.encode("utf-8"))
        return path
    except OSError as exc:
        warn(f"Could not write build HTML report: {exc}")
        return None


def _render_lineage_html_report(record: dict[str, Any],
                                dest: Path | None = None) -> Path | None:
    """Re-render one stored lineage record as a self-contained HTML report.

    `report html RUN_ID` entry point: everything needed to reproduce the
    delivery page already lives in the evidence state (lineage + the per-run
    audit JSON + optional provenance), so no rebuild and no cloud access is
    required. The catalog is resolved from the record's profile when
    possible; legacy records degrade to an empty catalog (rule rows then
    come from the audit results alone).
    """
    run_id = str(record.get("run_id") or "")
    if not run_id:
        warn("Cannot render HTML report: lineage record has no run_id")
        return None
    image_name = str(record.get("image_name") or "")
    # The audit JSON is archived next to the lineage as
    # <state-dir>/reports/<image-name>.<run-id>.json by the build/scan.
    audit_report: Path | None = None
    if image_name:
        cand = ohbs_image._reports_dir() / f"{image_name}.{run_id}.json"
        if cand.is_file():
            audit_report = cand
    if audit_report is None:
        for cand in sorted(ohbs_image._reports_dir().glob(f"*.{run_id}.json")):
            audit_report = cand
            break
    if audit_report is None:
        warn(f"No archived audit JSON for run {run_id} — the report will show "
             "structure only (no per-rule results)")
    # Best-effort profile -> role_dir lookup for catalog/guidance detail rows.
    role_dir = ""
    try:
        from ._profiles import PROFILES
        meta = PROFILES.get(str(record.get("profile") or ""))
        if isinstance(meta, dict):
            role_dir = str(meta.get("role_dir") or "")
    except Exception:
        role_dir = ""
    level = record.get("cis_level")
    try:
        level_i = int(level) if level is not None else 1
    except (TypeError, ValueError):
        level_i = 1
    image_ids = record.get("image_ids")
    if not isinstance(image_ids, list):
        image_ids = [str(image_ids)] if image_ids else []
    image_ids = [str(value) for value in image_ids]
    # A recorded provenance signature flips the release stamp to APPROVED.
    provenance: Path | None = None
    for image_id in image_ids:
        found = _find_provenance(image_id)
        if found:
            provenance = found[0]
            break
    ctx = _ReportContext(
        profile_name=str(record.get("profile") or ""),
        level=level_i,
        region=str(record.get("region") or ""),
        zone=str(record.get("zone") or ""),
        source_image_id=str(record.get("source_image_id") or ""),
        image_benchmark=str(record.get("benchmark") or "CIS"),
        run_id=run_id,
        role_dir=role_dir,
    )
    return _write_build_html_report(
        ctx, image_ids, image_name, record.get("score"),
        audit_report, provenance, signed=provenance is not None, dest=dest)


def _record_lineage(r: ResolvedConfig, image_ids: list[str], image_name: str,
                    score: float | None, ok: bool,
                    sbom_sha: str | None = None,
                    sbom_count: int | None = None,
                    mode: str = "build", run_id: str | None = None,
                    build_seconds: float | None = None) -> Path | None:
    """Append one lineage record. Returns the file path, or None on failure.

    *mode* — "build" (real hardening build), "scan" (audit-only) or "test"
    (idempotency run).  Readers that must only see real builds filter on it;
    records written before this field existed are treated as "build".

    *build_seconds* — wall-clock time of the Packer run (the build VM's
    billed lifetime), recorded so `report cost` can estimate spend without
    a billing API.  The instance type and spot flag always come from the
    resolved config.
    """
    if not isinstance(r, ResolvedConfig):
        return None  # defensive: only real resolved configs are recorded
    try:
        path = ohbs_image._lineage_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        from datetime import datetime
        rec = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_id": run_id or r.run_id or _new_run_id(),
            "status": "ok" if ok else "failed",
            "mode": mode,
            "ohbs_image_version": VERSION,
            "profile": r.profile_name,
            "cis_level": r.level,
            "region": r.region,
            "zone": r.zone,
            "source_image_id": r.source_image_id,
            "image_name": image_name,
            "image_ids": image_ids,
            "score": score,
            "scope": "scoped" if (r.rules_include or r.rules_exclude) else "full",
            # P1#4/#7 — benchmark pinning + change detection: the fingerprint
            # lets 'build --skip-if-unchanged' skip rebuilds when nothing
            # changed, and the benchmark name/version anchors the audit.
            "benchmark": r.image_benchmark,
            "fingerprint": ohbs_image._build_fingerprint(r),
            # #20 — the source image's CreatedTime at build time, so
            # 'ohbs-image check-source' can detect a vendor image refresh.
            "source_image_created": ohbs_image._source_image_created(r),
            # Cost facts (for `report cost`): the build VM's type, spot flag
            # and Packer wall time.  Legacy records predate these fields.
            "instance_type": getattr(r, "instance_type", None),
            "spot": bool(getattr(r, "spot", False)),
        }
        # P2#10 — SBOM pinning: hash + package count of the emitted SBOM.
        if sbom_sha:
            rec["sbom_sha256"] = sbom_sha
        if sbom_count is not None:
            rec["sbom_packages"] = sbom_count
        if build_seconds is not None and build_seconds >= 0:
            rec["build_seconds"] = round(build_seconds, 1)
        lock = _state_lock(path)
        try:
            with open(path, "a", encoding="utf-8") as fh:
                os.chmod(path, 0o600)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            lock.rmdir()
        return path
    except OSError:
        return None

def _bundled_rules_hash(role_dir: str, catalog: str = "rules.json") -> str:
    """SHA-256 of the bundled catalog for *role_dir* ("" if unavailable).

    *catalog* is the catalog basename (``rules.json`` for CIS, or
    ``rules_<slug>.json`` for a non-CIS benchmark).  Defaults to the CIS
    catalog to preserve prior callers.
    """
    import hashlib
    project_root = Path(__file__).parent.resolve()
    p = (project_root / "roles" / role_dir / "files" / catalog).resolve()
    try:
        p.relative_to((project_root / "roles").resolve())
    except ValueError:
        return ""
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def _file_hash(path: Path) -> str:
    """SHA-256 for an input file, recording a missing file deterministically."""
    import hashlib
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return f"missing:{path}"


def _test_components_hash(paths: list[str]) -> str:
    """Hash component scripts by path and content, so edits force a rebuild."""
    import hashlib
    entries = [
        {"path": str(Path(item).expanduser()),
         "sha256": _file_hash(Path(item).expanduser())}
        for item in paths
    ]
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _bundled_engine_hash(role_dir: str) -> str:
    """Hash the engine selected by a role; engine changes affect the image."""
    filename = "ohbs_engine.ps1" if role_dir.startswith("cis-win") else "ohbs_engine.py"
    return _file_hash(Path(__file__).parent / "roles" / role_dir / "files" / filename)

def _build_fingerprint(r: ResolvedConfig) -> str:
    """Deterministic fingerprint of every build input that affects the image.

    Includes every input that changes the resulting image, including engine
    and component-script content.  Stable across runs of the same inputs.
    """
    import hashlib
    if not isinstance(r, ResolvedConfig):
        return ""
    spec = r.build_spec
    parts = [
        "ohbs-image", VERSION,
        "profile", spec.profile_name,
        "level", str(spec.level),
        "region", spec.region,
        "zone", spec.zone,
        "source", spec.source_image_id,
        "instance", spec.instance_type,
        "benchmark", spec.benchmark,
        "os", spec.os_tag,
        "rules", ohbs_image._bundled_rules_hash(r.role_dir, spec.catalog_basename),
        "include", ",".join(r.rules_include),
        "exclude", ",".join(r.rules_exclude),
        "overrides", json.dumps(r.rules_overrides, sort_keys=True, ensure_ascii=False),
        "allow_disruptive", str(r.allow_disruptive),
        "smoke_test", str(r.smoke_test),
        "cve_scan", str(r.cve_scan),
        "sbom", str(r.sbom),
        "verify_boot", str(r.verify_boot),
        "spot", str(r.spot),
        "image_name_override", r.image_name_override,
        "ssh_debug_password", hashlib.sha256(r.ssh_debug_password.encode("utf-8")).hexdigest(),
        "packer_extra", json.dumps(r.packer_extra, sort_keys=True, ensure_ascii=False),
        "test_components", _test_components_hash(r.test_components),
        "engine", _bundled_engine_hash(r.role_dir),
        "templates", _file_hash(Path(__file__).with_name("_templates.py")),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

def _last_successful_fingerprint(r: ResolvedConfig) -> tuple[str | None, list[str]]:
    """Most recent 'ok' BUILD lineage record matching profile/level/region.

    Returns (fingerprint, image_ids) — None fingerprint when no match.
    Used by change detection to skip rebuilds with identical inputs.
    Only real builds count: audit-only scans and idempotency-test runs
    (mode "scan"/"test") produce images that are NOT hardened, so they must
    never satisfy 'build --skip-if-unchanged'.  Records written before the
    mode field existed (no "mode" key) are treated as builds.
    """
    path = ohbs_image._lineage_path()
    if not path.exists():
        return None, []
    match: dict[str, Any] | None = None
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if (rec.get("status") == "ok"
                    and rec.get("mode", "build") == "build"
                    and rec.get("profile") == r.profile_name
                    and rec.get("cis_level") == r.level
                    and rec.get("region") == r.region
                    and not rec.get("retired")):
                match = rec  # last matching record wins (file is append-only)
    if not match:
        return None, []
    return match.get("fingerprint"), list(match.get("image_ids") or [])

def _send_notification(r: ResolvedConfig, ok: bool, image_ids: list[str],
                       score: float | None, image_name: str) -> None:
    if not isinstance(r, ResolvedConfig):
        return
    # [notify].deploy_webhook — EventBridge-style downstream trigger (#14).
    # On SUCCESS, POST the image metadata to the customer's CI/CD so the
    # "new image is ready" event drives the deployment, not a human reading
    # the WeCom message.  Independent of the WeCom notification (fires even
    # when [notify].webhook is unset); never fails the build.
    if ok and r.deploy_webhook:
        ohbs_image._trigger_deploy_webhook(r, image_ids, score, image_name)
    if not r.notify_webhook:
        return
    if r.notify_on == "success" and not ok:
        return
    if r.notify_on == "failure" and ok:
        return
    if ok:
        head = "✅ ohbs-image build OK"
        body = (f"profile {r.profile_name} L{r.level} | image {image_name} | "
                f"score {score:g}%" if score is not None else
                f"profile {r.profile_name} L{r.level} | image {image_name}")
        if image_ids:
            body += f"\nimage-id: {', '.join(image_ids)}"
        body += f"\nregion {r.region}"
    else:
        head = "❌ ohbs-image build FAILED"
        body = f"profile {r.profile_name} L{r.level} | region {r.region} — check the build log"
    payload = json.dumps({"msgtype": "text", "text": {"content": f"{head}\n{body}"}},
                         ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            r.notify_webhook, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                info("Notification sent to WeCom webhook")
            else:
                warn(f"Notification webhook returned HTTP {resp.status}")
    except Exception as exc:  # notifications must never fail the build
        warn(f"Notification webhook failed: {exc}")

def _trigger_deploy_webhook(r: ResolvedConfig, image_ids: list[str],
                            score: float | None, image_name: str) -> None:
    """POST image metadata to [notify].deploy_webhook on build success."""
    payload = json.dumps({
        "event": "image.ready",
        "event_id": r.run_id,
        "image_id": (image_ids[0] if image_ids else ""),
        "image_ids": image_ids,
        "image_name": image_name,
        "profile": r.profile_name,
        "cis_level": r.level,
        "region": r.region,
        "benchmark": r.image_benchmark,
        "score": score,
        "ohbs_image_version": VERSION,
        "attestation_required": r.attestation_required,
    }, ensure_ascii=False).encode("utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                r.deploy_webhook, data=payload,
                headers={"Content-Type": "application/json", "Idempotency-Key": r.run_id}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status < 300:
                    ok(f"Deploy webhook triggered ({resp.status}, event {r.run_id})")
                    return
                raise OSError(f"returned HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            # A malformed/unauthorized request will not succeed by retrying;
            # 429 is explicitly retryable and 5xx is treated as transient.
            if 400 <= exc.code < 500 and exc.code != 429:
                warn(f"Deploy webhook rejected event without retry (HTTP {exc.code}): {exc}")
                return
            if attempt == 2:
                warn(f"Deploy webhook failed after 3 attempts: {exc}")
                return
            time.sleep(2 ** attempt)
        except Exception as exc:  # notifications must never fail the image build
            if attempt == 2:
                warn(f"Deploy webhook failed after 3 attempts: {exc}")
                return
            time.sleep(2 ** attempt)

def _write_provenance(r: ResolvedConfig, image_ids: list[str], image_name: str,
                      score: float | None,
                      sbom_sha: str | None = None,
                      sbom_count: int | None = None,
                      run_id: str | None = None) -> Path | None:
    if not isinstance(r, ResolvedConfig):
        return None
    try:
        from datetime import datetime
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        invocation_id = run_id or r.run_id or _new_run_id()
        dirp = ohbs_image._lineage_path().parent / "provenance"
        dirp.mkdir(parents=True, exist_ok=True, mode=0o700)
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", image_name) or "image"
        prov_path = dirp / f"{safe_name}.{invocation_id}.provenance.json"
        prov: dict[str, Any] = {
            "_type": "https://in-toto.io/Statement/v1",
            "predicateType": "https://slsa.dev/provenance/v1",
            # Cloud image IDs are provider identities, not content SHA-256
            # values. Never claim a fabricated sha256 digest in attestation.
            "subject": [{"name": i, "digest": {"tencentcloudImageId": i}} for i in image_ids],
            "predicate": {
                "buildDefinition": {
                    "buildType": "https://ohbs_image.dev/build/v1",
                    "externalParameters": {
                        "profile": r.profile_name,
                        "cis_level": r.level,
                        "region": r.region,
                        "zone": r.zone,
                        "source_image_id": r.source_image_id,
                        "instance_type": r.instance_type,
                        "benchmark": r.image_benchmark,
                        # Provider passthrough is privileged configuration.
                        # Preserve an auditable, non-secret summary without
                        # copying arbitrary operator-supplied values into the
                        # provenance document.
                        "packer_extra_keys": sorted(r.packer_extra),
                        "packer_extra_sha256": hashlib.sha256(
                            json.dumps(r.packer_extra, sort_keys=True,
                                       ensure_ascii=False).encode("utf-8")).hexdigest(),
                        # P1#4 — pin the exact rule catalog version that was
                        # applied, so the provenance is auditable against the
                        # benchmark edition (ansible-lockdown pins per-benchmark).
                        "rules_sha256": ohbs_image._bundled_rules_hash(r.role_dir, r.catalog_basename),
                        "fingerprint": ohbs_image._build_fingerprint(r),
                    },
                    "internalParameters": {"ohbs_image_version": VERSION},
                    "resolvedDependencies": [
                        {"uri": f"tencentcloud:cvm:image:{r.region}:{r.source_image_id}",
                         "digest": {"tencentcloudImageId": r.source_image_id}},
                        {"uri": f"pkg:generic/ohbs-rules/{r.role_dir}@{r.image_benchmark}",
                         "digest": {"sha256": ohbs_image._bundled_rules_hash(
                             r.role_dir, r.catalog_basename)}},
                    ],
                },
                "runDetails": {
                    "builder": {"id": f"ohbsimage@{VERSION}"},
                    "metadata": {
                        "invocationId": invocation_id,
                        "startedOn": ts,
                        "finishedOn": ts,
                    },
                },
            },
        }
        if score is not None:
            prov["predicate"]["runDetails"]["metadata"]["reAuditScore"] = score
        # P2#10 — SBOM pinning: the provenance now references the emitted
        # SBOM (hash + package count), closing the SLSA L2-style gap of
        # "what exactly shipped inside the image".
        if sbom_sha:
            prov["predicate"]["runDetails"]["metadata"]["sbomSha256"] = sbom_sha
        if sbom_count is not None:
            prov["predicate"]["runDetails"]["metadata"]["sbomPackageCount"] = sbom_count
        _atomic_write_bytes(
            prov_path, (json.dumps(prov, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
        if r.sign_key:
            sig = prov_path.with_suffix(prov_path.suffix + ".sig")
            try:
                rc = subprocess.run(
                    ["gpg", "--batch", "--yes", "--detach-sign", "--armor",
                     "--local-user", r.sign_key, "-o", str(sig), str(prov_path)],
                    capture_output=True, text=True, timeout=60)
                if rc.returncode == 0:
                    ok(f"Provenance signed with GPG key {r.sign_key} -> {sig.name}")
                else:
                    warn(f"GPG signing failed (key {r.sign_key}?): "
                         f"{(rc.stderr or rc.stdout).strip()[:200]}")
            except FileNotFoundError:
                warn("gpg not found — provenance written unsigned")
            except subprocess.TimeoutExpired:
                warn("gpg signing timed out — provenance written unsigned")
        return prov_path
    except OSError as exc:
        warn(f"Could not write provenance: {exc}")
        return None

def _find_provenance(image_id: str) -> list[Path]:
    """Locate provenance files whose subject references *image_id*."""
    dirp = ohbs_image._lineage_path().parent / "provenance"
    if not dirp.is_dir():
        return []
    hits: list[Path] = []
    for p in sorted(dirp.glob("*.provenance.json")):
        try:
            prov = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        subjects = [s.get("name", "") for s in prov.get("subject", [])]
        if image_id in subjects:
            hits.append(p)
    return hits
