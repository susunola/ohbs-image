from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from ._config import _state_dir
from ._logging import fail, info, ok


class StateBackend(Protocol):
    def push(self, source: Path) -> None: ...
    def pull(self, destination: Path) -> None: ...


def _copy_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = destination / ".ohbs-sync.lock"
    deadline = time.monotonic() + 10
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise OSError(f"timed out waiting for state sync lock {lock}") from None
            time.sleep(0.05)
    if not source.exists():
        lock.rmdir()
        return
    try:
        for item in source.rglob("*"):
            relative = item.relative_to(source)
            if item.name.endswith(".lock") or ".lock." in item.name:
                continue
            target = destination / relative
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            elif item.is_file():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temp = target.with_name(f".{target.name}.sync.tmp")
                shutil.copy2(item, temp)
                os.chmod(temp, 0o600)
                os.replace(temp, target)
    finally:
        with suppress(OSError):
            lock.rmdir()


class LocalStateBackend:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def push(self, source: Path) -> None:
        if self.root == source.resolve() or source.resolve() in self.root.parents:
            raise OSError("local backend must not be the state directory or one of its children")
        _copy_tree(source, self.root)

    def pull(self, destination: Path) -> None:
        _copy_tree(self.root, destination)


class CosStateBackend:
    """Tencent COS backend through the official coscli binary.

    Credentials stay in coscli's supported environment/config mechanisms;
    ohbs-image never places secrets on the command line.
    """
    def __init__(self, uri: str):
        if not uri.startswith("cos://") or ".." in uri:
            raise ValueError("COS URI must be cos://bucket/prefix without '..'")
        self.uri = uri.rstrip("/") + "/"
        if not shutil.which("coscli"):
            raise OSError("coscli not found; install the official Tencent Cloud COS CLI")

    def _sync(self, source: str, destination: str) -> None:
        command = ["coscli", "sync", "--recursive"]
        config_path = os.environ.get("OHBS_IMAGE_COSCLI_CONFIG", "").strip()
        if config_path:
            command += ["-c", str(Path(config_path).expanduser())]
        command += [source, destination]
        result = subprocess.run(command,
                                timeout=3600)
        if result.returncode != 0:
            raise OSError(f"coscli sync failed with exit code {result.returncode}")

    def push(self, source: Path) -> None:
        self._sync(str(source.resolve()) + "/", self.uri)

    def pull(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._sync(self.uri, str(destination.resolve()) + "/")


def _backend(kind: str, location: str) -> StateBackend:
    if kind == "local":
        return LocalStateBackend(Path(location))
    return CosStateBackend(location)


def cmd_state_sync(args: argparse.Namespace) -> int:
    try:
        backend = _backend(args.backend, args.location)
        root = _state_dir().expanduser().resolve()
        if args.direction == "push":
            backend.push(root)
        else:
            backend.pull(root)
    except (OSError, ValueError) as exc:
        fail(f"State sync failed: {exc}")
        return 1
    ok(f"State {args.direction} complete: {args.backend}:{args.location}")
    info(f"Local evidence root: {root}")
    return 0
