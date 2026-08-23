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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import ohbs_image

from ._config import ResolvedConfig
from ._logging import VERSION, info, ok, warn


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


def _read_run_manifest(run_id: str) -> dict[str, Any] | None:
    try:
        doc = json.loads(_run_manifest_path(run_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _write_run_manifest(r: ResolvedConfig, *, status: str, phase: str,
                        lease_hours: int = 48, resource: dict[str, str] | None = None,
                        notification: str | None = None) -> Path | None:
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
            current["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if status == "active":
                current["lease_expires_at"] = (datetime.now(UTC) + timedelta(hours=lease_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                current["lease_expires_at"] = current["updated_at"]
            if resource and resource not in current["resources"]:
                current["resources"].append(resource)
            if notification is not None:
                current["notification"] = notification
            _atomic_write_bytes(path, (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
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


def _write_build_html_report(r: ResolvedConfig, image_ids: list[str], image_name: str,
                             score: float | None, audit_report: Path | None,
                             provenance: Path | None, signed: bool) -> Path | None:
    """Write one portable, human-readable delivery report for an image build."""
    if not isinstance(r, ResolvedConfig):
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
    def text(value: object) -> str:
        return html.escape(str(value if value not in (None, "") else "—"))
    score_s = f"{score:g}%" if isinstance(score, (int, float)) else "—"
    status = "APPROVED" if signed or not r.attestation_required else "UNSIGNED"
    status_class = "approved" if status == "APPROVED" else "blocked"
    rows = [("Profile", r.profile_name), ("CIS level", f"L{r.level}"),
            ("Region / zone", f"{r.region} / {r.zone}"), ("Source image", r.source_image_id),
            ("Output image IDs", ", ".join(image_ids) or "—"), ("Benchmark", r.image_benchmark),
            ("Run ID", r.run_id), ("Audit report", str(audit_report or "—")),
            ("Provenance", str(provenance or "—"))]
    detail_rows = "".join(f"<tr><th>{text(k)}</th><td>{text(v)}</td></tr>" for k, v in rows)
    html_doc = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ohbs-image delivery report · {text(image_name)}</title><style>:root{{--ink:#15212c;--muted:#627487;--line:#dce4eb;--bg:#f3f6f8;--navy:#173a63;--ok:#06734d;--bad:#a12e2b}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}}main{{max-width:1000px;margin:auto;padding:32px 20px 64px}}header{{background:var(--navy);color:white;padding:30px;border-radius:15px 15px 0 0}}h1{{margin:0;font-size:27px}}.sub{{color:#cfdef0;margin-top:7px}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}}.card,table{{background:white;border:1px solid var(--line);border-radius:10px}}.card{{padding:17px}}.label{{font-size:11px;font-weight:800;letter-spacing:.8px;color:var(--muted);text-transform:uppercase}}.value{{font-size:27px;font-weight:800;margin-top:5px}}.approved{{color:var(--ok)}}.blocked{{color:var(--bad)}}h2{{font-size:18px;margin:32px 0 12px}}table{{border-collapse:separate;border-spacing:0;width:100%;overflow:hidden}}th,td{{padding:12px 15px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;word-break:break-word}}th{{width:31%;color:var(--muted);font-size:13px}}tr:last-child th,tr:last-child td{{border-bottom:0}}footer{{color:var(--muted);font-size:12px;margin-top:22px}}@media(max-width:700px){{.grid{{grid-template-columns:repeat(2,1fr)}}header{{border-radius:10px}}}}</style><body><main><header><h1>Image delivery report</h1><div class="sub">{text(image_name)} · generated by ohbs-image</div></header><section class="grid"><div class="card"><div class="label">Release status</div><div class="value {status_class}">{status}</div></div><div class="card"><div class="label">Re-audit score</div><div class="value">{text(score_s)}</div></div><div class="card"><div class="label">Rules passed</div><div class="value">{text(summary.get("pass", "—"))}</div></div><div class="card"><div class="label">Rules failed</div><div class="value">{text(summary.get("fail", "—"))}</div></div></section><h2>Build identity</h2><table>{detail_rows}</table><h2>Evidence</h2><table><tr><th>Attestation</th><td>{"Signed" if signed else "Not signed"}</td></tr><tr><th>SBOM packages</th><td>{text(summary.get("sbom_packages", "—"))}</td></tr><tr><th>Audit mode</th><td>{text(audit.get("mode", "—"))}</td></tr></table><footer>This report is a human-readable view. Verify the referenced provenance signature and machine-readable result for release automation.</footer></main></body></html>'''
    try:
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", image_name) or "image"
        path = ohbs_image._reports_dir() / f"{safe_name}.{r.run_id}.html"
        _atomic_write_bytes(path, html_doc.encode("utf-8"))
        return path
    except OSError as exc:
        warn(f"Could not write build HTML report: {exc}")
        return None

def _record_lineage(r: ResolvedConfig, image_ids: list[str], image_name: str,
                    score: float | None, ok: bool,
                    sbom_sha: str | None = None,
                    sbom_count: int | None = None,
                    mode: str = "build", run_id: str | None = None) -> Path | None:
    """Append one lineage record. Returns the file path, or None on failure.

    *mode* — "build" (real hardening build), "scan" (audit-only) or "test"
    (idempotency run).  Readers that must only see real builds filter on it;
    records written before this field existed are treated as "build".
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
        }
        # P2#10 — SBOM pinning: hash + package count of the emitted SBOM.
        if sbom_sha:
            rec["sbom_sha256"] = sbom_sha
        if sbom_count is not None:
            rec["sbom_packages"] = sbom_count
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
    parts = [
        "ohbs-image", VERSION,
        "profile", r.profile_name,
        "level", str(r.level),
        "region", r.region,
        "zone", r.zone,
        "source", r.source_image_id,
        "instance", r.instance_type,
        "benchmark", r.image_benchmark,
        "os", r.image_os_tag,
        "rules", ohbs_image._bundled_rules_hash(r.role_dir, r.catalog_basename),
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
            "subject": [{"name": i, "digest": {"sha256": "n/a"}} for i in image_ids],
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
