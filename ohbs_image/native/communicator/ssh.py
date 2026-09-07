"""OpenSSH-based communicator with strict disconnect semantics."""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

from ..._logging import ConfigError

_REMOTE_STARTED = "__OHBS_NATIVE_REMOTE_STARTED__"


def _control_path(ip: str, port: int, user: str, key_path: str) -> str:
    identity = f"{user}@{ip}:{port}:{key_path}".encode()
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    # macOS limits AF_UNIX paths to 104 bytes. Its per-user temporary directory
    # is already ~75 bytes, so putting the socket beside the ephemeral key makes
    # OpenSSH/scp fail before connecting. /tmp is the stable short alias for
    # /private/tmp; UID + connection digest keeps concurrent users/runs isolated.
    return f"/tmp/ohbs-ssh-{os.getuid()}-{suffix}.sock"


def _known_hosts_path(key_path: str) -> str:
    """Keep TOFU state isolated to the ephemeral keypair/build directory."""
    return str(Path(key_path).parent / "known_hosts")


def _options(ip: str, port: int, user: str, key_path: str) -> list[str]:
    return ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={_known_hosts_path(key_path)}",
            "-o", "HashKnownHosts=yes", "-o", "Compression=yes",
            "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=30", "-o", "ControlMaster=auto",
            "-o", "ControlPersist=60", "-o",
            f"ControlPath={_control_path(ip, port, user, key_path)}", "-i", key_path]


def _base(ip: str, port: int, user: str, key_path: str) -> list[str]:
    return ["ssh", *_options(ip, port, user, key_path), "-p", str(port),
            f"{user}@{ip}"]


def ready(ip: str, port: int, user: str, *, key_path: str,
          timeout_s: int = 600) -> bool:
    deadline = time.monotonic() + timeout_s
    attempt = 0
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                [*_base(ip, port, user, key_path), "true"], capture_output=True,
                text=True, timeout=min(20, max(1, int(deadline - time.monotonic()))))
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        # Most CVMs become reachable shortly after RUNNING. Probe quickly at
        # first, then back off to keep long boots inexpensive.
        time.sleep(min(10, 2 + attempt, max(0, deadline - time.monotonic())))
        attempt += 1
    return False


def upload(local: Path, destination: str, *, ip: str, port: int,
           user: str, key_path: str, timeout: int) -> list[str]:
    cmd = ["scp", "-q", *_options(ip, port, user, key_path), "-P", str(port),
           str(local), f"{user}@{ip}:{destination}"]
    # Large Ansible bundles can legitimately take longer than 30 seconds on a
    # 1 Mbps build link. Keep the retry loop bounded by the global build budget.
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(1, int(deadline - time.monotonic()))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            if time.monotonic() < deadline:
                continue
            raise ConfigError(
                f"native file upload timed out for {user}@{ip}:{destination}") from exc
        if not result.returncode:
            return [f"native: uploaded {local.name} -> {destination}"]
        if result.returncode != 255 or time.monotonic() >= deadline:
            detail = (result.stderr or result.stdout).strip()[:500]
            raise ConfigError(f"native file upload failed: {detail}")
        time.sleep(3)


def execute(ip: str, port: int, user: str, key_path: str, command: str,
            *, timeout: int, disconnect_ok: bool = False) -> list[str]:
    deadline = time.monotonic() + timeout
    wire_command = command
    if disconnect_ok:
        wire_command = f"printf '{_REMOTE_STARTED}\\n' && {command}"
    while True:
        remaining = max(1, int(deadline - time.monotonic()))
        result = subprocess.run([*_base(ip, port, user, key_path), wire_command],
                                capture_output=True, text=True, timeout=remaining)
        lines = (result.stdout + result.stderr).splitlines()
        remote_started = _REMOTE_STARTED in result.stdout.splitlines()
        lines = [line for line in lines if line != _REMOTE_STARTED]
        if not result.returncode:
            return lines
        if disconnect_ok and result.returncode == 255 and remote_started:
            return lines
        # Replaying a command after an authenticated connection disappears is
        # unsafe: the guest may have completed it and only lost the response.
        # Retry only errors that prove no remote session was established.
        detail = "\n".join(lines).lower()
        definitely_not_started = any(marker in detail for marker in (
            "connection refused", "no route to host", "network is unreachable",
            "operation timed out", "connection timed out",
        ))
        if result.returncode == 255 and not definitely_not_started:
            tail = "\n".join(lines[-20:])
            raise ConfigError(
                "native remote command outcome is ambiguous after SSH disconnect; "
                f"refusing automatic replay:\n{tail}")
        if result.returncode != 255 or time.monotonic() >= deadline:
            tail = "\n".join(lines[-20:])
            raise ConfigError(
                f"native remote provisioner failed ({result.returncode}):\n{tail}")
        time.sleep(3)


def path_for_user(path: str, user: str) -> str:
    if user != "root" and not path.startswith(("/tmp/", "/home/")):
        return f"/home/{user}/.{Path(path).name}.ohbs-stage"
    return path
