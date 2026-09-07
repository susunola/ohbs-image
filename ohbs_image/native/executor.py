"""Cloud-neutral provisioner executor for OHBS Native Engine."""
from __future__ import annotations

import base64
import json
import shlex
import signal
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from .._config import PackerResult
from .._failures import classify_failure
from .._logging import ConfigError
from .compiler import inline_script_content
from .contracts import CommunicatorOps, ProviderOps
from .evidence import write_build_record
from .journal import heartbeat_journal, read_journal, verify_resume_identity, write_journal
from .spec import BuildSpec, ProvisionerSpec

_REMOTE_MARKER = "/var/lib/ohbs-image/native-state.json"
_REMOTE_CACHE_ROOT = "/var/cache/ohbs-image/sha256"
_CACHE_HIT = "__OHBS_NATIVE_CACHE_HIT__"
_HEARTBEAT_INTERVAL_SECONDS = 15.0


def _install_signal_handlers() -> dict[signal.Signals, Any]:
    """Convert normal process termination into a catchable cleanup path."""
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[signal.Signals, Any] = {}

    def interrupted(signum: int, _frame: Any) -> None:
        name = signal.Signals(signum).name
        raise InterruptedError(f"native build interrupted by {name}")

    for value in (signal.SIGINT, signal.SIGTERM):
        previous[value] = signal.getsignal(value)
        signal.signal(value, interrupted)
    return previous


def _restore_signal_handlers(previous: dict[signal.Signals, Any]) -> None:
    for value, handler in previous.items():
        signal.signal(value, handler)


def _remaining(deadline: float, *, phase: str = "build") -> int:
    value = max(1, int(deadline - time.monotonic()))
    if value <= 1:
        raise ConfigError(f"native phase {phase} time budget exhausted")
    return value


def _label(item: ProvisionerSpec, index: int) -> str:
    return item.label if item.label != "inline" else f"inline-{index}"


def _integrity_command(path: str, digest: str) -> str:
    if not digest:
        return ""
    return (f"printf '%s  %s\\n' {shlex.quote(digest)} {shlex.quote(path)} "
            "| sha256sum -c -")


def _cache_path(digest: str) -> str:
    return f"{_REMOTE_CACHE_ROOT}/{digest}"


def _cache_probe_command(digest: str) -> str:
    cached = shlex.quote(_cache_path(digest))
    check = _integrity_command(_cache_path(digest), digest)
    return (f"if sudo test -f {cached} && {check} >/dev/null 2>&1; "
            f"then printf '{_CACHE_HIT}\\n'; fi")


def _cache_materialize_command(digest: str, destination: str, user: str) -> str:
    return (f"sudo install -D -m 0600 -o {shlex.quote(user)} "
            f"{shlex.quote(_cache_path(digest))} {shlex.quote(destination)}")


def _cache_store_command(digest: str, source: str) -> str:
    return (f"sudo install -D -m 0600 {shlex.quote(source)} "
            f"{shlex.quote(_cache_path(digest))}")


def _marker_document(*, run_id: str, plan_digest: str,
                     completed: set[int], phase: str) -> dict[str, Any]:
    return {
        "schema": "https://ohbs-image.dev/native-remote-marker/v1",
        "run_id": run_id,
        "plan_sha256": plan_digest,
        "completed_provisioners": sorted(completed),
        "phase": phase,
        "updated_at_epoch": int(time.time()),
    }


def _write_marker_command(document: dict[str, Any]) -> str:
    payload = base64.b64encode(json.dumps(
        document, sort_keys=True, separators=(",", ":")).encode()).decode()
    target = shlex.quote(_REMOTE_MARKER)
    return ("sudo install -d -m 0700 /var/lib/ohbs-image && "
            f"printf %s {shlex.quote(payload)} | base64 -d | "
            f"sudo tee {target}.tmp >/dev/null && "
            f"sudo chmod 0600 {target}.tmp && sudo mv {target}.tmp {target}")


def _read_marker(lines: list[str]) -> dict[str, Any] | None:
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if (isinstance(value, dict) and value.get("schema") ==
                "https://ohbs-image.dev/native-remote-marker/v1"):
            return value
    return None


def execute_build(
    spec: BuildSpec,
    workdir: Path,
    runtime: Any,
    *,
    timeout: int,
    provider: ProviderOps,
    communicator: CommunicatorOps,
    plan_digest: str,
    emit: Callable[[str], None] | None = None,
    resume: bool = False,
    retain_on_failure: bool = False,
) -> PackerResult:
    """Execute one compiled BuildSpec without importing a cloud SDK."""
    build_started = time.monotonic()
    started_at_epoch = int(time.time())
    deadline = build_started + timeout
    lines: list[str] = []
    instance_id = key_id = key_path = ""
    image_ids: list[str] = []
    phase = "prepare"
    phase_started = build_started
    phase_durations: dict[str, float] = {}
    phase_budgets: dict[str, float] = {}
    phase_deadline = deadline
    phase_limits = dict(getattr(runtime, "native_phase_timeout_minutes", {}) or {})
    cleanup_errors: list[str] = []
    cleanup_error_types: list[str] = []
    cleanup_remaining: list[dict[str, str]] = []
    succeeded = False
    completed: set[int] = set()
    provisioner_results: list[dict[str, Any]] = []
    active_result: dict[str, Any] | None = None
    active_started = 0.0
    outcome: PackerResult | None = None
    failure_evidence: dict[str, Any] = {}
    signal_handlers = _install_signal_handlers()
    journal_active = False
    heartbeat_stop = threading.Event()
    heartbeat_stats = {"interval_seconds": _HEARTBEAT_INTERVAL_SECONDS,
                       "writes": 0, "errors": 0}
    heartbeat_worker: threading.Thread | None = None
    transfer_cache = {
        "eligible_files": 0, "hits": 0, "misses": 0,
        "uploaded_bytes": 0, "saved_bytes": 0,
    }

    def record(message: str) -> None:
        lines.append(message)
        if emit:
            emit(message)

    def start_phase(name: str, budget_key: str | None = None) -> None:
        nonlocal phase, phase_started, phase_deadline
        now = time.monotonic()
        phase_durations[phase] = phase_durations.get(phase, 0.0) + now - phase_started
        phase = name
        phase_started = now
        limit = phase_limits.get(budget_key or name)
        phase_deadline = min(deadline, now + limit * 60) if limit else deadline
        phase_budgets[name] = max(0.0, phase_deadline - now)
        if journal_active:
            write_journal(workdir, instance_id=instance_id, phase=phase,
                          status="started")

    def start_heartbeat() -> None:
        nonlocal heartbeat_worker

        def run() -> None:
            while not heartbeat_stop.is_set():
                try:
                    if heartbeat_journal(workdir):
                        heartbeat_stats["writes"] += 1
                except Exception:
                    heartbeat_stats["errors"] += 1
                if heartbeat_stop.wait(_HEARTBEAT_INTERVAL_SECONDS):
                    break

        heartbeat_worker = threading.Thread(
            target=run, name=f"ohbs-native-heartbeat-{str(runtime.run_id)[:8]}",
            daemon=True)
        heartbeat_worker.start()

    try:
        if resume:
            journal = read_journal(workdir)
            if journal is None:
                raise ConfigError("native resume journal is missing or invalid")
            expected = {
                "run_id": runtime.run_id, "image_name": spec.image_name,
                "source_image_id": spec.target.source_image_id,
                "plan_sha256": plan_digest,
            }
            mismatched = verify_resume_identity(journal, expected)
            if mismatched:
                raise ConfigError("native resume identity mismatch: " + ", ".join(mismatched))
            instance_id = str(journal.get("instance_id") or "")
            key_id = str(journal.get("key_id") or "")
            key_path = str(journal.get("key_path") or "")
            if not all((instance_id, key_id, key_path)) or not Path(key_path).is_file():
                raise ConfigError("native resume resources are incomplete or key is unavailable")
            completed = {int(value) for value in journal.get("completed_provisioners", [])
                         if isinstance(value, int) or str(value).isdigit()}
            journal_active = True
            start_heartbeat()
            record(f"native: resuming instance {instance_id} after {len(completed)} "
                   "completed provisioner(s)")
        else:
            start_phase("keypair")
            key_id, key_path, _ = provider.setup_keypair(runtime)
            # Close the key-only crash window before invoking RunInstances.
            write_journal(
                workdir, instance_id="", phase=phase, status="completed",
                metadata={
                    "run_id": runtime.run_id, "image_name": spec.image_name,
                    "source_image_id": spec.target.source_image_id,
                    "plan_sha256": plan_digest, "provider": provider.name,
                    "communicator": communicator.name, "key_id": key_id,
                    "key_path": key_path, "completed_provisioners": [],
                    "remote_marker": _REMOTE_MARKER,
                })
            journal_active = True
            start_heartbeat()
            start_phase("launch")
            instance_id = provider.launch(runtime, key_id, spec.image_name)
            _remaining(phase_deadline, phase=phase)
            write_journal(
                workdir, instance_id=instance_id, phase=phase, status="completed",
                metadata={
                    "run_id": runtime.run_id, "image_name": spec.image_name,
                    "source_image_id": spec.target.source_image_id,
                    "plan_sha256": plan_digest, "provider": provider.name,
                    "communicator": communicator.name, "key_id": key_id,
                    "key_path": key_path, "completed_provisioners": [],
                    "remote_marker": _REMOTE_MARKER,
                })
            record(f"native: instance {instance_id} launched via {provider.name}")

        start_phase("connect")
        instance = provider.wait_instance(runtime, instance_id, "RUNNING", phase_deadline)
        ip = provider.instance_ip(instance)
        if not ip:
            raise ConfigError("native build instance has no reachable IP address")
        active_user = runtime.ssh_username
        budget = _remaining(phase_deadline, phase=phase)
        if not communicator.ready(
                ip, runtime.ssh_port, active_user, key_path=key_path,
                timeout_s=min(30, budget) if resume else budget):
            if not resume or not communicator.ready(
                    ip, runtime.ssh_port, "ohbsimage", key_path=key_path,
                    timeout_s=_remaining(phase_deadline, phase=phase)):
                raise ConfigError(f"native SSH did not become ready on {ip}:{runtime.ssh_port}")
            active_user = "ohbsimage"
            record("native: resumed with ohbsimage + sudo (root SSH is locked)")
        record(f"native: {communicator.name} ready on {ip}:{runtime.ssh_port} as {active_user}")

        # Journals created before remote markers remain resumable. New journals
        # require local and remote agreement before a provisioner is skipped.
        if resume and journal is not None and journal.get("remote_marker") == _REMOTE_MARKER:
            marker_lines = communicator.execute(
                ip, runtime.ssh_port, active_user, key_path,
                f"sudo cat {shlex.quote(_REMOTE_MARKER)} 2>/dev/null || true",
                timeout=_remaining(phase_deadline, phase=phase))
            marker = _read_marker(marker_lines)
            if marker is None:
                completed.clear()
                record("native: remote recovery marker missing; safely rerunning provisioners")
            else:
                marker_mismatch = verify_resume_identity(marker, {
                    "run_id": runtime.run_id, "plan_sha256": plan_digest,
                })
                if marker_mismatch:
                    raise ConfigError("native remote marker identity mismatch: " +
                                      ", ".join(marker_mismatch))
                remote_completed = {
                    int(value) for value in marker.get("completed_provisioners", [])
                    if isinstance(value, int) or str(value).isdigit()
                }
                local_completed = set(completed)
                completed.intersection_update(remote_completed)
                if completed != local_completed or completed != remote_completed:
                    record("native: local/remote recovery markers differed; using safe intersection")

        for index, item in enumerate(spec.provisioners, 1):
            if index in completed:
                record(f"native: provisioner {index}/{len(spec.provisioners)} already completed; skipped")
                provisioner_results.append({
                    "index": index, "kind": item.kind, "label": _label(item, index),
                    "status": "recovered", "duration_seconds": 0.0,
                })
                continue
            start_phase(f"provisioner-{index}",
                        "reboot" if item.expect_disconnect else "provision")
            write_journal(workdir, instance_id=instance_id, phase=phase,
                          status="started", provisioner=index)
            started = time.monotonic()
            active_started = started
            active_result = {
                "index": index, "kind": item.kind, "label": _label(item, index),
                "status": "started", "started_at_epoch": int(time.time()),
            }
            provisioner_results.append(active_result)
            record(f"native: provisioner {index}/{len(spec.provisioners)} "
                   f"{item.kind} {_label(item, index)} started")
            if item.pause_before:
                time.sleep(item.pause_before)
            if item.expect_disconnect and not communicator.ready(
                    ip, runtime.ssh_port, active_user, key_path=key_path,
                    timeout_s=max(1, int(min(
                        phase_deadline - time.monotonic(),
                        item.start_retry_timeout or 600)))):
                raise ConfigError("native SSH did not recover before post-reboot upload")
            remaining = _remaining(phase_deadline, phase=phase)
            if item.kind == "file":
                local = Path(workdir) / item.source
                desired = item.destination
                destination = communicator.path_for_user(desired, active_user)
                size = local.stat().st_size
                cache_hit = False
                if item.cacheable and item.content_sha256:
                    transfer_cache["eligible_files"] += 1
                    try:
                        probe = communicator.execute(
                            ip, runtime.ssh_port, active_user, key_path,
                            _cache_probe_command(item.content_sha256),
                            timeout=remaining)
                        if _CACHE_HIT in probe:
                            communicator.execute(
                                ip, runtime.ssh_port, active_user, key_path,
                                _cache_materialize_command(
                                    item.content_sha256, destination, active_user),
                                timeout=remaining)
                            cache_hit = True
                    except ConfigError:
                        cache_hit = False
                if cache_hit:
                    transfer_cache["hits"] += 1
                    transfer_cache["saved_bytes"] += size
                    record(f"native: content cache hit {item.content_sha256[:12]} "
                           f"({size} bytes saved)")
                else:
                    if item.cacheable:
                        transfer_cache["misses"] += 1
                    try:
                        lines.extend(communicator.upload(
                            local, destination, ip=ip, port=runtime.ssh_port,
                            user=active_user, key_path=key_path, timeout=remaining))
                    except ConfigError:
                        if active_user != "root":
                            raise
                        active_user = "ohbsimage"
                        destination = communicator.path_for_user(desired, active_user)
                        record("native: root SSH locked; switched to ohbsimage + sudo")
                        lines.extend(communicator.upload(
                            local, destination, ip=ip, port=runtime.ssh_port,
                            user=active_user, key_path=key_path, timeout=remaining))
                    transfer_cache["uploaded_bytes"] += size
                verify = _integrity_command(destination, item.content_sha256)
                if item.cacheable and not cache_hit:
                    store = _cache_store_command(item.content_sha256, destination)
                    verify = f"{verify} && ({store} || true)" if verify else f"({store} || true)"
                if destination != desired:
                    mode = "0700" if local.stat().st_mode & 0o111 else "0600"
                    install = (f"sudo install -D -m {mode} {shlex.quote(destination)} "
                               f"{shlex.quote(desired)} && rm -f {shlex.quote(destination)}")
                    lines.extend(communicator.execute(
                        ip, runtime.ssh_port, active_user, key_path,
                        f"{verify} && {install}" if verify else install,
                        timeout=remaining))
                elif verify:
                    lines.extend(communicator.execute(
                        ip, runtime.ssh_port, active_user, key_path, verify,
                        timeout=remaining))
            else:
                desired = item.remote_path or f"/tmp/ohbs-native-{index}.sh"
                remote = communicator.path_for_user(desired, active_user)
                generated = not item.script
                if generated:
                    with tempfile.NamedTemporaryFile(
                            "w", encoding="utf-8", delete=False,
                            prefix="ohbs-native-", suffix=".sh") as handle:
                        handle.write(inline_script_content(item.inline))
                    local = Path(handle.name)
                else:
                    local = Path(workdir) / item.script
                try:
                    try:
                        lines.extend(communicator.upload(
                            local, remote, ip=ip, port=runtime.ssh_port,
                            user=active_user, key_path=key_path, timeout=remaining))
                    except ConfigError:
                        if active_user != "root":
                            raise
                        active_user = "ohbsimage"
                        remote = communicator.path_for_user(desired, active_user)
                        record("native: root SSH locked; switched to ohbsimage + sudo")
                        lines.extend(communicator.upload(
                            local, remote, ip=ip, port=runtime.ssh_port,
                            user=active_user, key_path=key_path, timeout=remaining))
                    target = remote
                    verify = _integrity_command(remote, item.content_sha256)
                    prepare = f"chmod 0700 {shlex.quote(remote)}"
                    if remote != desired:
                        prepare += (f" && sudo install -D -m 0700 {shlex.quote(remote)} "
                                    f"{shlex.quote(desired)} && rm -f {shlex.quote(remote)}")
                        target = desired
                    if verify:
                        prepare = f"{verify} && {prepare}"
                    lines.extend(communicator.execute(
                        ip, runtime.ssh_port, active_user, key_path,
                        f"{prepare} && sudo {shlex.quote(target)}", timeout=remaining,
                        disconnect_ok=item.expect_disconnect))
                finally:
                    if generated:
                        local.unlink(missing_ok=True)
            record(f"native: provisioner {index}/{len(spec.provisioners)} completed "
                   f"in {time.monotonic() - started:.1f}s")
            active_result.update({
                "status": "completed",
                "duration_seconds": round(time.monotonic() - started, 3),
                "finished_at_epoch": int(time.time()),
            })
            active_result = None
            write_journal(workdir, instance_id=instance_id, phase=phase,
                          status="completed", provisioner=index)
            completed.add(index)
            lines.extend(communicator.execute(
                ip, runtime.ssh_port, active_user, key_path,
                _write_marker_command(_marker_document(
                    run_id=runtime.run_id, plan_digest=plan_digest,
                    completed=completed, phase=phase)),
                timeout=_remaining(phase_deadline, phase=phase)))

        start_phase("snapshot")
        record("native: all provisioners completed; creating image")
        image_id = provider.create_image(
            runtime, instance_id, spec.image_name, phase_deadline)
        # Preserve the primary artifact immediately. Cross-region sync may
        # fail after the image already exists and incurs storage cost; its ID
        # must survive in failure evidence for reconciliation and release triage.
        image_ids = [image_id]
        write_journal(workdir, instance_id=instance_id, phase=phase, status="completed",
                      detail=image_id)
        start_phase("copy-images", "sync")
        copies = provider.copy_images(runtime, image_id, phase_deadline)
        image_ids.extend(copies)
        for value in image_ids:
            record(f"Created image ID: {value}")
        rendered = " ".join(
            [f"{spec.target.region}: {image_id}",
             *(f"{region}: {copy_id}" for region, copy_id
               in zip(runtime.image_copy_regions, copies, strict=True))])
        record(f"--> native.{provider.name}: images({rendered}) were created.")
        succeeded = True
        outcome = PackerResult(0, lines)
        return outcome
    except Exception as exc:
        failure = classify_failure(str(exc), phase=phase)
        failure_evidence = {
            "category": failure.category.value,
            "code": failure.code,
            "retryable": failure.retryable,
            "phase": phase,
            "error_type": type(exc).__name__,
        }
        if active_result is not None and active_result.get("status") == "started":
            active_result.update({
                "status": "failed", "finished_at_epoch": int(time.time()),
                "duration_seconds": round(time.monotonic() - active_started, 3),
                "failure_category": failure.category.value,
            })
        record(str(exc))
        if instance_id:
            with suppress(OSError):
                write_journal(workdir, instance_id=instance_id, phase=phase,
                              status="failed", detail=str(exc))
        outcome = PackerResult(1, lines, failure_category=failure.category.value,
                               retryable=failure.retryable)
        return outcome
    finally:
        # A second signal during cleanup should retain normal process semantics;
        # restore the caller's handlers before touching cloud resources.
        _restore_signal_handlers(signal_handlers)
        now = time.monotonic()
        phase_durations[phase] = phase_durations.get(phase, 0.0) + now - phase_started
        cleanup_started = now
        if journal_active:
            with suppress(OSError):
                write_journal(workdir, instance_id=instance_id, phase="cleanup",
                              status="started")
        retain = bool(instance_id and not succeeded and retain_on_failure)
        if retain:
            record(f"native: retained failed instance {instance_id} for explicit resume")
        if instance_id and not retain:
            try:
                provider.terminate(runtime, instance_id)
            except Exception as exc:
                message = f"instance cleanup failed for {instance_id}: {exc}"
                cleanup_errors.append(message)
                cleanup_error_types.append(type(exc).__name__)
                cleanup_remaining.append({"type": "instance", "id": instance_id})
                record(f"native: {message}")
        if (key_id or key_path) and not retain:
            try:
                provider.teardown_keypair(runtime, key_id, key_path)
            except Exception as exc:
                message = f"keypair cleanup failed for {key_id or key_path}: {exc}"
                cleanup_errors.append(message)
                cleanup_error_types.append(type(exc).__name__)
                if key_id:
                    cleanup_remaining.append({"type": "keypair", "id": key_id})
                record(f"native: {message}")
        if cleanup_errors and outcome is not None and outcome.exit_code == 0:
            # Image creation succeeded, but declaring the whole build successful
            # would hide billable or credential-bearing leaked resources. Do not
            # mark retryable: rebuilding would create another image and more cost.
            outcome.exit_code = 1
            outcome.failure_category = "provider"
            outcome.retryable = False
            succeeded = False
            phase = "cleanup"
            failure_evidence = {
                "category": "provider", "code": "cleanup-incomplete",
                "retryable": False, "phase": "cleanup",
                "error_type": cleanup_error_types[0] if cleanup_error_types else "CleanupError",
            }
            record("native: image was created, but lifecycle cleanup was incomplete")
            with suppress(OSError):
                write_journal(workdir, instance_id=instance_id, phase=phase,
                              status="failed", detail="cleanup incomplete")
        finished_cleanup = time.monotonic()
        heartbeat_stop.set()
        if heartbeat_worker is not None:
            heartbeat_worker.join(timeout=5)
        if journal_active:
            with suppress(OSError):
                write_journal(
                    workdir, instance_id=instance_id,
                    phase="snapshot" if succeeded else phase,
                    status="completed" if succeeded else "failed")
        phase_durations["cleanup"] = finished_cleanup - cleanup_started
        phase_durations["total"] = finished_cleanup - build_started
        try:
            provider_evidence = dict(getattr(
                runtime, "_native_provider_evidence", {}) or {})
            provider_evidence["transfer_cache"] = transfer_cache
            provider_evidence["journal_heartbeat"] = heartbeat_stats
            capacity_decision = getattr(runtime, "_native_capacity_decision", None)
            if isinstance(capacity_decision, dict):
                provider_evidence["capacity_decision"] = capacity_decision
            evidence = write_build_record(
                workdir, spec, run_id=str(runtime.run_id), provider=provider.name,
                communicator=communicator.name, instance_id=instance_id,
                image_ids=image_ids, status="completed" if succeeded else "failed",
                failure_phase=phase, phase_durations=phase_durations,
                started_at_epoch=started_at_epoch, cleanup_errors=cleanup_errors,
                provider_evidence=provider_evidence,
                provisioner_results=provisioner_results,
                cleanup_remaining=cleanup_remaining,
                failure=failure_evidence, phase_budgets=phase_budgets)

            record(f"native: build record -> {evidence}")
        except OSError as exc:
            record(f"native: could not write build record: {exc}")
        try:
            log_path = Path(workdir) / "native" / "build.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass
