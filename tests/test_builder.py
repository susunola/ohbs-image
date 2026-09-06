import hashlib
import json
import signal
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from ohbs_image._builder import native_plan, select_builder, validate_native
from ohbs_image._cli import build_parser
from ohbs_image._logging import ConfigError
from ohbs_image._native import _scp, _ssh, parse_native_provisioners, run_native
from ohbs_image._reports import (
    _audit_identity_failures,
    _coverage_explanation,
    _rules_for_level,
)
from ohbs_image._tc_cloud import _create_temporary_ingress, _delete_temporary_ingress
from ohbs_image._templates import HCL_LINUX_TEMPLATE
from ohbs_image.native.communicator.ssh import _base, _control_path
from ohbs_image.native.compiler import (
    compile_hcl_provisioners,
    compile_workspace,
    inline_script_content,
)
from ohbs_image.native.contracts import CommunicatorOps, ProviderOps
from ohbs_image.native.evidence import _api_summary
from ohbs_image.native.executor import (
    _install_signal_handlers,
    _integrity_command,
    _read_marker,
    _restore_signal_handlers,
    _write_marker_command,
    execute_build,
)
from ohbs_image.native.journal import heartbeat_journal, write_journal
from ohbs_image.native.providers import list_providers, provider_capabilities
from ohbs_image.native.providers.tencentcloud import (
    _call,
    _validate_output_image,
    _validate_source_image,
    _wait_image_normal,
    copy_images,
    create_image,
    launch,
    teardown_keypair,
    terminate,
    wait_instance,
)
from ohbs_image.native.spec import BuildSpec, CloudTarget, ProvisionerSpec


def _resolved(**overrides):
    values = {
        "family": "",
        "profile_name": "tencentos4",
        "source_image_id": "img-source",
        "region": "ap-guangzhou",
        "zone": "ap-guangzhou-4",
        "instance_type": "S5.MEDIUM2",
        "assume_role_arn": "",
        "packer_extra": {},
        "role_dir": "cis-tencentos4",
        "image_os_tag": "tencentos-4",
        "image_benchmark": "CIS-v1.0.0",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_native_plan_is_stable_and_written(tmp_path: Path):
    packer = tmp_path / "packer"
    packer.mkdir()
    (packer / "main.pkr.hcl").write_text(
        'build { provisioner "shell" { inline = ["echo ok"] } }', encoding="utf-8")
    plan = native_plan(tmp_path, _resolved(), "gold-l1")
    assert plan.schema_version == 2
    assert plan.provider == "tencentcloud-cvm"
    assert plan.communicator == "ssh"
    assert plan.provisioners == (
        "connect", "01:shell:inline-1", "snapshot", "copy-images", "cleanup")
    assert (tmp_path / "native" / "plan.json").is_file()
    assert (tmp_path / "native" / "build-spec.json").is_file()


def test_native_validate_requires_rendered_contract(tmp_path: Path):
    with mock.patch("ohbs_image._builder.shutil.which", return_value="/usr/bin/tool"):
        result = validate_native(tmp_path, _resolved(), "gold-l1")
    assert result.exit_code == 1
    assert "missing rendered input" in "\n".join(result.stdout_lines)


def test_native_validate_rejects_unparseable_generated_hcl(tmp_path: Path):
    required = (
        "packer/auto.pkrvars.hcl", "packer/ansible-bundle.tar.gz",
        "packer/scripts/install-ansible.sh",
        "packer/scripts/ohbs-image-finalize.sh",
    )
    for rel in required:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("input", encoding="utf-8")
    hcl = tmp_path / "packer/main.pkr.hcl"
    hcl.write_text('build { provisioner "shell" { inline = ["broken" } }',
                   encoding="utf-8")
    with mock.patch("ohbs_image._builder.shutil.which", return_value="/usr/bin/tool"):
        result = validate_native(tmp_path, _resolved(), "gold-l1")
    assert result.exit_code == 1
    assert "native execution plan is invalid" in "\n".join(result.stdout_lines)


def test_builder_switch_defaults_to_packer_and_accepts_native():
    parser = build_parser()
    assert parser.parse_args(["validate"]).builder == "packer"
    assert parser.parse_args(["validate", "--builder", "native"]).builder == "native"
    assert parser.parse_args(["validate", "--builder", "auto"]).builder == "auto"


def test_auto_builder_uses_native_for_supported_linux_and_packer_for_gaps():
    assert select_builder(_resolved(), "auto") == "native"
    assert select_builder(_resolved(family="windows"), "auto") == "packer"
    assert select_builder(_resolved(assume_role_arn="qcs::role"), "auto") == "packer"
    assert select_builder(_resolved(packer_extra={"unsupported_field": True}), "auto") == "packer"


def test_tencent_provider_capabilities_are_registered_with_aliases():
    capability = provider_capabilities("tencentcloud-cvm")
    assert capability.name == "tencentcloud"
    assert capability.supports_image_copy is True
    assert [item.name for item in list_providers()] == ["tencentcloud"]


def test_native_subpackages_are_in_distribution_manifest():
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    packages = set(project["tool"]["setuptools"]["packages"])
    assert {
        "ohbs_image.native", "ohbs_image.native.communicator",
        "ohbs_image.native.providers",
    } <= packages


def test_compiled_build_spec_hashes_uploaded_content(tmp_path: Path):
    packer = tmp_path / "packer"
    packer.mkdir()
    payload = tmp_path / "payload.txt"
    payload.write_text("stable content", encoding="utf-8")
    catalog = tmp_path / "ansible/roles/cis-tencentos4/files/rules.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"benchmark":"CIS-v1.0.0"}', encoding="utf-8")
    (packer / "main.pkr.hcl").write_text(
        'build { provisioner "file" { source = "payload.txt" destination = "/tmp/x" } }',
        encoding="utf-8")
    spec = compile_workspace(tmp_path, _resolved(), "gold-l1")
    assert len(spec.provisioners[0].content_sha256) == 64
    assert spec.schema_version == 3
    assert spec.provisioners[0].cacheable is False
    assert spec.benchmark == "CIS-v1.0.0"
    assert len(spec.catalog_sha256) == 64


def test_compiler_marks_only_generated_ansible_bundle_cacheable(tmp_path: Path):
    packer = tmp_path / "packer"
    packer.mkdir()
    bundle = packer / "ansible-bundle.tar.gz"
    bundle.write_bytes(b"stable role archive")
    (packer / "main.pkr.hcl").write_text(
        'build { provisioner "file" { source = "packer/ansible-bundle.tar.gz" '
        'destination = "/tmp/bundle.tar.gz" } }', encoding="utf-8")
    spec = compile_workspace(tmp_path, _resolved(), "gold-l1")
    assert spec.schema_version == 3
    assert spec.provisioners[0].cacheable is True


@pytest.mark.parametrize(
    ("cacheable", "cache_hit", "expected_uploads", "expected_hits", "expected_misses"),
    [(True, True, 0, 1, 0), (True, False, 1, 0, 1),
     (False, True, 1, 0, 0)],
)
def test_native_content_cache_is_verified_and_sensitive_files_bypass_it(
        tmp_path: Path, cacheable: bool, cache_hit: bool,
        expected_uploads: int, expected_hits: int, expected_misses: int):
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"immutable payload")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    key = tmp_path / "key"
    key.write_text("private", encoding="utf-8")
    spec = BuildSpec(
        schema_version=3, profile="portable-linux", image_name="gold",
        target=CloudTarget("fakecloud", "region-a", "zone-a", "source-1", "small"),
        provisioners=(ProvisionerSpec(
            kind="file", source="payload.bin", destination="/tmp/payload.bin",
            content_sha256=digest, cacheable=cacheable),),
    )
    provider = ProviderOps(
        setup_keypair=lambda runtime: ("key-1", str(key), "pub"),
        launch=lambda *args: "vm-1",
        wait_instance=lambda *args: {"PublicIpAddresses": ["203.0.113.9"]},
        instance_ip=lambda instance: instance["PublicIpAddresses"][0],
        create_image=lambda *args: "image-1", copy_images=lambda *args: [],
        terminate=lambda *args: None, teardown_keypair=lambda *args: None,
        name="fakecloud")
    uploads: list[str] = []
    commands: list[str] = []

    def execute(*args, **kwargs):
        command = args[4]
        commands.append(command)
        if "__OHBS_NATIVE_CACHE_HIT__" in command and cache_hit:
            return ["__OHBS_NATIVE_CACHE_HIT__"]
        return []

    communicator = CommunicatorOps(
        ready=lambda *args, **kwargs: True,
        upload=lambda *args, **kwargs: uploads.append(str(args[0])) or ["uploaded"],
        execute=execute, path_for_user=lambda path, user: path, name="ssh")
    runtime = _resolved(
        run_id="12345678-1234-1234-1234-123456789abc", ssh_username="root",
        ssh_port=22, image_copy_regions=[])
    result = execute_build(
        spec, tmp_path, runtime, timeout=60, provider=provider,
        communicator=communicator, plan_digest="digest")
    assert result.exit_code == 0
    assert len(uploads) == expected_uploads
    record = json.loads((tmp_path / "native/build-record.json").read_text())
    cache = record["provider_evidence"]["transfer_cache"]
    assert cache["hits"] == expected_hits
    assert cache["misses"] == expected_misses
    assert cache["saved_bytes"] == (payload.stat().st_size if expected_hits else 0)
    if not cacheable:
        assert not any("/var/cache/ohbs-image" in command for command in commands)
    elif not cache_hit:
        assert any("sudo install -D -m 0600" in command and
                   "/var/cache/ohbs-image" in command for command in commands)


def test_compiled_build_spec_hashes_inline_commands():
    first = compile_hcl_provisioners(
        'build { provisioner "shell" { inline = ["echo one"] } }')[0]
    second = compile_hcl_provisioners(
        'build { provisioner "shell" { inline = ["echo two"] } }')[0]
    assert len(first.content_sha256) == 64
    assert first.content_sha256 != second.content_sha256
    assert first.content_sha256 == hashlib.sha256(
        inline_script_content(first.inline).encode()).hexdigest()
    command = _integrity_command("/tmp/a file", first.content_sha256)
    assert "sha256sum -c -" in command
    assert "'/tmp/a file'" in command


def test_inline_digest_verifies_the_exact_generated_script_bytes(tmp_path: Path):
    item = compile_hcl_provisioners(
        'build { provisioner "shell" { inline = ["set -e", "echo ready"] } }')[0]
    script = tmp_path / "generated script.sh"
    script.write_text(inline_script_content(item.inline), encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", _integrity_command(str(script), item.content_sha256)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_cloud_neutral_executor_runs_with_non_tencent_provider(tmp_path: Path):
    key = tmp_path / "key"
    key.write_text("private", encoding="utf-8")
    spec = BuildSpec(
        schema_version=1, profile="portable-linux", image_name="gold",
        target=CloudTarget("fakecloud", "region-a", "zone-a", "source-1", "small"),
        provisioners=(ProvisionerSpec(kind="shell", inline=("echo portable",)),),
    )

    def fail_cleanup(_runtime, _instance_id):
        raise OSError("temporary provider outage")

    provider = ProviderOps(
        setup_keypair=lambda runtime: ("key-1", str(key), "pub"),
        launch=lambda runtime, key_id, image_name: "vm-1",
        wait_instance=lambda runtime, instance_id, wanted, deadline: {
            "PublicIpAddresses": ["203.0.113.9"]},
        instance_ip=lambda instance: instance["PublicIpAddresses"][0],
        create_image=lambda runtime, instance_id, image_name, deadline: "image-1",
        copy_images=lambda runtime, image_id, deadline: [],
        terminate=fail_cleanup,
        teardown_keypair=lambda runtime, key_id, key_path: None,
        name="fakecloud")
    communicator = CommunicatorOps(
        ready=lambda *args, **kwargs: True,
        upload=lambda *args, **kwargs: ["uploaded"],
        execute=lambda *args, **kwargs: ["portable"],
        path_for_user=lambda path, user: path,
        name="ssh")
    runtime = _resolved(
        run_id="12345678-1234-1234-1234-123456789abc", ssh_username="root",
        ssh_port=22, image_copy_regions=[])
    runtime._native_provider_evidence = {"api_requests": [{
        "action": "RunInstances", "region": "ap-guangzhou",
        "request_id": "req-1", "duration_ms": 12.5, "attempts": 2,
        "status": "success",
    }]}
    result = execute_build(
        spec, tmp_path, runtime, timeout=60, provider=provider,
        communicator=communicator, plan_digest="digest")
    assert result.exit_code == 1
    assert result.failure_category == "provider"
    assert result.retryable is False
    assert any("native.fakecloud" in line for line in result.stdout_lines)
    record = json.loads((tmp_path / "native/build-record.json").read_text())
    assert record["status"] == "failed"
    assert record["provider"] == "fakecloud"
    assert record["failure"] == {
        "category": "provider", "code": "cleanup-incomplete", "retryable": False,
        "phase": "cleanup", "error_type": "OSError",
    }
    assert record["image_ids"] == ["image-1"]
    assert record["build_spec_sha256"]
    assert record["phase_duration_seconds"]["total"] >= 0
    assert record["provisioner_results"][0]["status"] == "completed"
    assert record["provisioner_results"][0]["duration_seconds"] >= 0
    assert record["cleanup"]["status"] == "failed"
    assert record["cleanup"]["remaining_resources"] == [
        {"type": "instance", "id": "vm-1"}]
    assert record["phase_duration_seconds"]["cleanup"] >= 0
    assert record["provider_evidence"]["api_summary"]["retries"] == 1
    assert record["provider_evidence"]["api_summary"]["p95_duration_ms"] == 12.5
    assert "temporary provider outage" in record["cleanup"]["errors"][0]
    assert any("cleanup failed" in line for line in result.stdout_lines)


def test_native_signal_handler_converts_sigterm_to_cleanup_exception():
    handlers = _install_signal_handlers()
    try:
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(InterruptedError, match="SIGTERM"):
            handler(signal.SIGTERM, None)
    finally:
        _restore_signal_handlers(handlers)


def test_native_journals_key_before_launch_to_close_crash_window(tmp_path: Path):
    key = tmp_path / "key"
    key.write_text("private", encoding="utf-8")
    observed: dict[str, object] = {}

    def fail_launch(*args):
        observed.update(json.loads((tmp_path / "native/journal.json").read_text()))
        raise ConfigError("launch rejected")

    provider = ProviderOps(
        setup_keypair=lambda runtime: ("key-early", str(key), "pub"),
        launch=fail_launch,
        wait_instance=lambda *args: {}, instance_ip=lambda instance: "",
        create_image=lambda *args: "", copy_images=lambda *args: [],
        terminate=lambda *args: None, teardown_keypair=lambda *args: None,
        name="fakecloud")
    communicator = CommunicatorOps(
        ready=lambda *args, **kwargs: True, upload=lambda *args, **kwargs: [],
        execute=lambda *args, **kwargs: [], path_for_user=lambda path, user: path,
        name="ssh")
    spec = BuildSpec(
        schema_version=1, profile="portable", image_name="gold",
        target=CloudTarget("fakecloud", "r", "z", "source", "small"),
        provisioners=(ProvisionerSpec(kind="shell", inline=("true",)),))
    runtime = _resolved(run_id="run-early", ssh_username="root", ssh_port=22,
                        image_copy_regions=[])
    result = execute_build(
        spec, tmp_path, runtime, timeout=60, provider=provider,
        communicator=communicator, plan_digest="plan")
    assert result.exit_code == 1
    assert observed["key_id"] == "key-early"
    assert observed["run_id"] == "run-early"
    assert observed["instance_id"] == ""


def test_native_journal_heartbeat_preserves_phase_identity_and_checkpoints(tmp_path: Path):
    write_journal(
        tmp_path, instance_id="vm-1", phase="provisioner-2", status="started",
        provisioner=2, metadata={
            "run_id": "run-1", "plan_sha256": "plan-1",
            "completed_provisioners": [1],
        })
    assert heartbeat_journal(tmp_path) is True
    assert heartbeat_journal(tmp_path) is True
    journal = json.loads((tmp_path / "native/journal.json").read_text())
    assert journal["phase"] == "provisioner-2"
    assert journal["status"] == "started"
    assert journal["provisioner"] == 2
    assert journal["completed_provisioners"] == [1]
    assert journal["run_id"] == "run-1"
    assert journal["plan_sha256"] == "plan-1"
    assert journal["heartbeat_sequence"] == 2
    assert journal["heartbeat_at_epoch"] == journal["updated_at_epoch"]


def test_native_phase_budgets_bound_provider_deadlines_and_enter_evidence(tmp_path: Path):
    key = tmp_path / "key"
    key.write_text("private", encoding="utf-8")
    observed: dict[str, float] = {}

    def wait_instance(runtime, instance_id, wanted, deadline):
        observed["connect"] = deadline
        return {"PublicIpAddresses": ["203.0.113.9"]}

    def create_image(runtime, instance_id, image_name, deadline):
        observed["snapshot"] = deadline
        return "image-1"

    def copy_images(runtime, image_id, deadline):
        observed["sync"] = deadline
        return []

    provider = ProviderOps(
        setup_keypair=lambda runtime: ("key-1", str(key), "pub"),
        launch=lambda *args: "vm-1", wait_instance=wait_instance,
        instance_ip=lambda instance: instance["PublicIpAddresses"][0],
        create_image=create_image, copy_images=copy_images,
        terminate=lambda *args: None, teardown_keypair=lambda *args: None,
        name="fakecloud")
    communicator = CommunicatorOps(
        ready=lambda *args, **kwargs: True, upload=lambda *args, **kwargs: [],
        execute=lambda *args, **kwargs: [], path_for_user=lambda path, user: path,
        name="ssh")
    runtime = _resolved(
        run_id="12345678-1234-1234-1234-123456789abc", ssh_username="root",
        ssh_port=22, image_copy_regions=[], native_phase_timeout_minutes={
            "connect": 1, "snapshot": 2, "sync": 3,
        })
    started = __import__("time").monotonic()
    result = execute_build(
        BuildSpec(
            schema_version=3, profile="portable", image_name="gold",
            target=CloudTarget("fakecloud", "r", "z", "source", "small"),
            provisioners=()),
        tmp_path, runtime, timeout=600, provider=provider,
        communicator=communicator, plan_digest="plan")
    assert result.exit_code == 0
    assert 0 < observed["connect"] - started <= 60.1
    assert 60 < observed["snapshot"] - started <= 120.1
    assert 120 < observed["sync"] - started <= 180.1
    record = json.loads((tmp_path / "native/build-record.json").read_text())
    budgets = record["phase_budget_seconds"]
    assert 59 <= budgets["connect"] <= 60
    assert 119 <= budgets["snapshot"] <= 120
    assert 179 <= budgets["copy-images"] <= 180


@pytest.mark.parametrize(
    ("failure_stage", "expected_phase", "expected_images"),
    [("cancel", "connect", []), ("snapshot", "snapshot", []),
     ("sync", "copy-images", ["image-1"])],
)
def test_native_release_faults_always_cleanup_and_preserve_created_image(
        tmp_path: Path, failure_stage: str, expected_phase: str,
        expected_images: list[str]):
    key = tmp_path / "key"
    key.write_text("private", encoding="utf-8")
    terminate = mock.Mock()
    teardown = mock.Mock()

    def wait_instance(*args):
        if failure_stage == "cancel":
            raise InterruptedError("native build interrupted by SIGTERM")
        return {"PublicIpAddresses": ["203.0.113.9"]}

    def create_image(*args):
        if failure_stage == "snapshot":
            raise ConfigError("image entered failure state CREATEFAILED")
        return "image-1"

    def copy_images(*args):
        if failure_stage == "sync":
            raise ConfigError("copied image verification failed")
        return []

    provider = ProviderOps(
        setup_keypair=lambda runtime: ("key-1", str(key), "pub"),
        launch=lambda *args: "vm-1", wait_instance=wait_instance,
        instance_ip=lambda instance: instance["PublicIpAddresses"][0],
        create_image=create_image, copy_images=copy_images,
        terminate=terminate, teardown_keypair=teardown, name="fakecloud")
    communicator = CommunicatorOps(
        ready=lambda *args, **kwargs: True, upload=lambda *args, **kwargs: [],
        execute=lambda *args, **kwargs: [], path_for_user=lambda path, user: path,
        name="ssh")
    runtime = _resolved(
        run_id="12345678-1234-1234-1234-123456789abc", ssh_username="root",
        ssh_port=22, image_copy_regions=[])
    result = execute_build(
        BuildSpec(
            schema_version=3, profile="portable", image_name="gold",
            target=CloudTarget("fakecloud", "r", "z", "source", "small"),
            provisioners=()),
        tmp_path, runtime, timeout=60, provider=provider,
        communicator=communicator, plan_digest="plan")
    assert result.exit_code == 1
    terminate.assert_called_once_with(runtime, "vm-1")
    teardown.assert_called_once_with(runtime, "key-1", str(key))
    record = json.loads((tmp_path / "native/build-record.json").read_text())
    assert record["failure_phase"] == expected_phase
    assert record["failure"]["phase"] == expected_phase
    assert record["image_ids"] == expected_images


def test_resume_uses_safe_intersection_of_local_and_remote_markers(tmp_path: Path):
    key = tmp_path / "key"
    key.write_text("private", encoding="utf-8")
    spec = BuildSpec(
        schema_version=1, profile="portable-linux", image_name="gold",
        target=CloudTarget("fakecloud", "region-a", "zone-a", "source-1", "small"),
        provisioners=(
            ProvisionerSpec(kind="shell", inline=("echo one",)),
            ProvisionerSpec(kind="shell", inline=("echo two",)),
        ),
    )
    run_id = "12345678-1234-1234-1234-123456789abc"
    native = tmp_path / "native"
    native.mkdir()
    (native / "journal.json").write_text(json.dumps({
        "schema": "https://ohbs-image.dev/native-journal/v1",
        "run_id": run_id, "image_name": "gold", "source_image_id": "source-1",
        "plan_sha256": "digest", "instance_id": "vm-1", "key_id": "key-1",
        "key_path": str(key), "completed_provisioners": [1, 2],
        "remote_marker": "/var/lib/ohbs-image/native-state.json",
    }), encoding="utf-8")
    provider = ProviderOps(
        setup_keypair=lambda runtime: (_ for _ in ()).throw(AssertionError()),
        launch=lambda *args: (_ for _ in ()).throw(AssertionError()),
        wait_instance=lambda *args: {"PublicIpAddresses": ["203.0.113.9"]},
        instance_ip=lambda instance: instance["PublicIpAddresses"][0],
        create_image=lambda *args: "image-1", copy_images=lambda *args: [],
        terminate=lambda *args: None, teardown_keypair=lambda *args: None,
        name="fakecloud")
    uploads: list[str] = []
    remote = json.dumps({
        "schema": "https://ohbs-image.dev/native-remote-marker/v1",
        "run_id": run_id, "plan_sha256": "digest",
        "completed_provisioners": [1], "phase": "provisioner-1",
    })

    def execute(*args, **kwargs):
        return [remote] if "sudo cat" in args[4] else ["ok"]

    communicator = CommunicatorOps(
        ready=lambda *args, **kwargs: True,
        upload=lambda local, *args, **kwargs: uploads.append(local.name) or ["uploaded"],
        execute=execute, path_for_user=lambda path, user: path, name="ssh")
    runtime = _resolved(run_id=run_id, ssh_username="root", ssh_port=22,
                        image_copy_regions=[])
    result = execute_build(
        spec, tmp_path, runtime, timeout=60, provider=provider,
        communicator=communicator, plan_digest="digest", resume=True)
    assert result.exit_code == 0
    assert len(uploads) == 1
    assert any("safe intersection" in line for line in result.stdout_lines)


def test_resume_rejects_legacy_hcl_digest_instead_of_compiled_spec_digest(tmp_path: Path):
    key = tmp_path / "key"
    key.write_text("private", encoding="utf-8")
    spec = BuildSpec(
        schema_version=2, profile="portable", image_name="gold",
        target=CloudTarget("fake", "r", "z", "source", "small"),
        provisioners=(ProvisionerSpec(kind="shell", inline=("true",),
                                      content_sha256="new-compiler-digest"),))
    native = tmp_path / "native"
    native.mkdir()
    (native / "journal.json").write_text(json.dumps({
        "schema": "https://ohbs-image.dev/native-journal/v1",
        "run_id": "run-1", "image_name": "gold", "source_image_id": "source",
        "plan_sha256": hashlib.sha256(b"same raw hcl").hexdigest(),
        "instance_id": "vm-1", "key_id": "key-1", "key_path": str(key),
    }), encoding="utf-8")
    wait_instance = mock.Mock()
    provider = ProviderOps(
        setup_keypair=mock.Mock(), launch=mock.Mock(), wait_instance=wait_instance,
        instance_ip=mock.Mock(), create_image=mock.Mock(), copy_images=mock.Mock(),
        terminate=mock.Mock(), teardown_keypair=mock.Mock(), name="fake")
    communicator = CommunicatorOps(
        ready=mock.Mock(), upload=mock.Mock(), execute=mock.Mock(),
        path_for_user=mock.Mock(), name="ssh")
    runtime = _resolved(run_id="run-1", ssh_username="root", ssh_port=22,
                        image_copy_regions=[])
    result = execute_build(
        spec, tmp_path, runtime, timeout=60, provider=provider,
        communicator=communicator, plan_digest=spec.sha256(), resume=True)
    assert result.exit_code == 1
    assert any("plan_sha256" in line for line in result.stdout_lines)
    wait_instance.assert_not_called()


def test_generated_hcl_subset_parses_in_order():
    hcl = '''
    build {
      provisioner "file" { source = "bundle.tgz" destination = "/tmp/bundle.tgz" }
      provisioner "shell" {
        pause_before = "5s"
        start_retry_timeout = "20m"
        remote_path = "/tmp/run.sh"
        inline = ["set -e", "echo ready"]
        expect_disconnect = true
      }
    }
    '''
    parsed = parse_native_provisioners(hcl)
    assert [item.kind for item in parsed] == ["file", "shell"]
    assert parsed[1].inline == ("set -e", "echo ready")
    assert parsed[1].pause_before == 5
    assert parsed[1].start_retry_timeout == 1200
    assert parsed[1].expect_disconnect is True


def test_current_linux_template_is_native_parseable():
    parsed = parse_native_provisioners(HCL_LINUX_TEMPLATE)
    assert len(parsed) >= 17
    assert sum(item.expect_disconnect for item in parsed) == 2
    assert any(item.kind == "file" for item in parsed)


def test_ssh_retries_only_when_remote_command_definitely_did_not_start():
    disconnected = SimpleNamespace(returncode=255, stdout="", stderr="Connection refused")
    succeeded = SimpleNamespace(returncode=0, stdout="ready\n", stderr="")
    with (
        mock.patch("ohbs_image.native.communicator.ssh.subprocess.run",
                   side_effect=[disconnected, succeeded]) as run,
        mock.patch("ohbs_image.native.communicator.ssh.time.sleep"),
    ):
        assert _ssh("203.0.113.8", 22, "root", "/tmp/key", "echo ready", timeout=60) == [
            "ready"
        ]
    assert run.call_count == 2


def test_ssh_refuses_to_replay_command_after_ambiguous_disconnect():
    disconnected = SimpleNamespace(returncode=255, stdout="", stderr="Connection closed")
    with (
        mock.patch("ohbs_image.native.communicator.ssh.subprocess.run",
                   return_value=disconnected) as run,
        pytest.raises(ConfigError, match="outcome is ambiguous"),
    ):
        _ssh("203.0.113.8", 22, "root", "/tmp/key", "apply-hardening", timeout=60)
    run.assert_called_once()


def test_ssh_uses_stable_short_control_socket():
    first = _control_path("203.0.113.8", 22, "root", "/tmp/run/key.pem")
    second = _control_path("203.0.113.8", 22, "root", "/tmp/run/key.pem")
    assert first == second
    assert first.startswith("/tmp/ohbs-ssh-")
    assert len(first.encode()) < 104
    long_key = "/var/folders/" + "very-long-component/" * 12 + "key.pem"
    assert len(_control_path("203.0.113.8", 22, "root", long_key).encode()) < 104
    command = _base("203.0.113.8", 22, "root", "/tmp/run/key.pem")
    assert "ControlMaster=auto" in command
    assert "ControlPersist=60" in command
    assert "StrictHostKeyChecking=accept-new" in command
    assert "UserKnownHostsFile=/tmp/run/known_hosts" in command
    assert "HashKnownHosts=yes" in command
    assert "Compression=yes" in command
    assert "StrictHostKeyChecking=no" not in command
    assert "UserKnownHostsFile=/dev/null" not in command


def test_remote_marker_command_is_private_atomic_and_parseable():
    document = {
        "schema": "https://ohbs-image.dev/native-remote-marker/v1",
        "run_id": "run-1", "plan_sha256": "abc",
        "completed_provisioners": [1, 2], "phase": "provisioner-2",
    }
    command = _write_marker_command(document)
    assert "install -d -m 0700 /var/lib/ohbs-image" in command
    assert "chmod 0600" in command
    assert "native-state.json.tmp" in command
    assert _read_marker(["noise", json.dumps(document)]) == document


def test_expected_disconnect_does_not_mask_remote_script_failure():
    failed = SimpleNamespace(returncode=1, stdout="", stderr="reboot command failed")
    with (
        mock.patch("ohbs_image.native.communicator.ssh.subprocess.run", return_value=failed),
        pytest.raises(ConfigError, match="remote provisioner failed"),
    ):
        _ssh("203.0.113.8", 22, "root", "/tmp/key", "exit 1", timeout=60,
             disconnect_ok=True)


def test_expected_disconnect_accepts_ssh_transport_loss():
    disconnected = SimpleNamespace(
        returncode=255, stdout="__OHBS_NATIVE_REMOTE_STARTED__\n",
        stderr="Connection closed")
    with mock.patch("ohbs_image.native.communicator.ssh.subprocess.run",
                    return_value=disconnected):
        assert _ssh("203.0.113.8", 22, "root", "/tmp/key", "reboot", timeout=60,
                    disconnect_ok=True) == ["Connection closed"]


def test_expected_disconnect_rejects_auth_failure_before_remote_start():
    rejected = SimpleNamespace(
        returncode=255, stdout="", stderr="Permission denied (publickey)")
    with (
        mock.patch("ohbs_image.native.communicator.ssh.subprocess.run",
                   return_value=rejected) as run,
        pytest.raises(ConfigError, match="outcome is ambiguous"),
    ):
        _ssh("203.0.113.8", 22, "root", "/tmp/key", "reboot", timeout=60,
             disconnect_ok=True)
    assert "__OHBS_NATIVE_REMOTE_STARTED__" in run.call_args.args[0][-1]


def test_scp_retries_transient_transport_disconnect(tmp_path: Path):
    disconnected = SimpleNamespace(returncode=255, stdout="", stderr="Connection closed")
    succeeded = SimpleNamespace(returncode=0, stdout="", stderr="")
    payload = tmp_path / "payload"
    payload.write_text("ok", encoding="utf-8")
    with (
        mock.patch("ohbs_image.native.communicator.ssh.subprocess.run",
                   side_effect=[disconnected, succeeded]) as run,
        mock.patch("ohbs_image.native.communicator.ssh.time.sleep"),
    ):
        assert _scp(payload, "/tmp/payload", ip="203.0.113.8", port=22,
                    user="root", key_path="/tmp/key", timeout=60) == [
            "native: uploaded payload -> /tmp/payload"
        ]
    assert run.call_count == 2


def test_scp_timeout_becomes_config_error_for_user_fallback(tmp_path: Path):
    payload = tmp_path / "payload"
    payload.write_text("ok", encoding="utf-8")
    with (
        mock.patch("ohbs_image.native.communicator.ssh.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(["scp"], 1)),
        mock.patch("ohbs_image.native.communicator.ssh.time.monotonic",
                   side_effect=[0, 59, 61]),
        pytest.raises(ConfigError, match="native file upload timed out"),
    ):
        _scp(payload, "/tmp/payload", ip="203.0.113.8", port=22,
             user="root", key_path="/tmp/key", timeout=60)


def test_native_build_cleans_instance_and_key(tmp_path: Path):
    packer = tmp_path / "packer"
    packer.mkdir()
    (packer / "main.pkr.hcl").write_text(
        'build { provisioner "shell" { inline = ["echo ok"] } }', encoding="utf-8")
    resolved = _resolved(
        run_id="12345678-1234-1234-1234-123456789abc",
        secret_id_env="SID", secret_key_env="SKEY", security_token_env="TOKEN",
        ssh_port=22, ssh_username="root", spot=False,
        image_copy_regions=[],
    )
    with (
        mock.patch("ohbs_image._probe_setup_keypair", return_value=("skey-1", "/tmp/key", "pub")),
        mock.patch("ohbs_image._probe_ssh_ready", return_value=True),
        mock.patch("ohbs_image._native._terminate") as terminate,
        mock.patch("ohbs_image._native._teardown_keypair") as teardown,
        mock.patch("ohbs_image._native._native_launch", return_value="ins-1"),
        mock.patch("ohbs_image._native._wait_instance_state",
                   return_value={"InstanceState": "RUNNING", "PublicIpAddresses": ["203.0.113.8"]}),
        mock.patch("ohbs_image._native._scp", return_value=["uploaded"]),
        mock.patch("ohbs_image._native._ssh", return_value=["Score: 100.0%"]),
        mock.patch("ohbs_image._native._create_image", return_value="img-1"),
        mock.patch("ohbs_image._native._copy_images", return_value=[]),
    ):
        result = run_native(tmp_path, resolved, "gold-l1", timeout=600)
    assert result.exit_code == 0
    assert "Created image ID: img-1" in result.stdout_lines
    terminate.assert_called_once_with(resolved, "ins-1")
    teardown.assert_called_once_with(resolved, "skey-1", "/tmp/key")
    journal = __import__("json").loads((tmp_path / "native/journal.json").read_text())
    assert journal["phase"] == "snapshot"
    assert journal["status"] == "completed"
    assert journal["detail"] == "img-1"


def test_native_build_emits_provisioner_lifecycle(tmp_path: Path):
    packer = tmp_path / "packer"
    packer.mkdir()
    (packer / "main.pkr.hcl").write_text(
        'build { provisioner "shell" { inline = ["echo ok"] } }', encoding="utf-8")
    resolved = _resolved(
        run_id="12345678-1234-1234-1234-123456789abc",
        secret_id_env="SID", secret_key_env="SKEY", security_token_env="TOKEN",
        ssh_port=22, ssh_username="root", spot=False, image_copy_regions=[],
    )
    emitted: list[str] = []
    with (
        mock.patch("ohbs_image._probe_setup_keypair", return_value=("skey-1", "/tmp/key", "pub")),
        mock.patch("ohbs_image._probe_ssh_ready", return_value=True),
        mock.patch("ohbs_image._native._terminate"),
        mock.patch("ohbs_image._native._teardown_keypair"),
        mock.patch("ohbs_image._native._native_launch", return_value="ins-1"),
        mock.patch("ohbs_image._native._wait_instance_state",
                   return_value={"InstanceState": "RUNNING", "PublicIpAddresses": ["203.0.113.8"]}),
        mock.patch("ohbs_image._native._scp", return_value=["uploaded"]),
        mock.patch("ohbs_image._native._ssh", return_value=["Score: 100.0%"]),
        mock.patch("ohbs_image._native._create_image", return_value="img-1"),
        mock.patch("ohbs_image._native._copy_images", return_value=[]),
    ):
        result = run_native(tmp_path, resolved, "gold-l1", timeout=600, emit=emitted.append)
    assert result.exit_code == 0
    assert any("provisioner 1/1 shell" in line and "started" in line for line in emitted)
    assert any("provisioner 1/1 completed" in line for line in emitted)


def test_native_failure_can_explicitly_retain_resources_for_resume(tmp_path: Path):
    packer = tmp_path / "packer"
    packer.mkdir()
    hcl = 'build { provisioner "shell" { inline = ["exit 1"] } }'
    (packer / "main.pkr.hcl").write_text(hcl, encoding="utf-8")
    resolved = _resolved(
        run_id="12345678-1234-1234-1234-123456789abc",
        secret_id_env="SID", secret_key_env="SKEY", security_token_env="TOKEN",
        ssh_port=22, ssh_username="root", spot=False, image_copy_regions=[])
    with (
        mock.patch("ohbs_image._probe_setup_keypair", return_value=("skey-1", "/tmp/key", "pub")),
        mock.patch("ohbs_image._probe_ssh_ready", return_value=True),
        mock.patch("ohbs_image._native._terminate") as terminate,
        mock.patch("ohbs_image._native._teardown_keypair") as teardown,
        mock.patch("ohbs_image._native._native_launch", return_value="ins-1"),
        mock.patch("ohbs_image._native._wait_instance_state",
                   return_value={"InstanceState": "RUNNING", "PublicIpAddresses": ["203.0.113.8"]}),
        mock.patch("ohbs_image._native._scp", return_value=["uploaded"]),
        mock.patch("ohbs_image._native._ssh", side_effect=ConfigError("remote exit 1")),
    ):
        result = run_native(tmp_path, resolved, "gold-l1", timeout=600,
                            retain_on_failure=True)
    assert result.exit_code == 1
    terminate.assert_not_called()
    teardown.assert_not_called()
    journal = __import__("json").loads((tmp_path / "native/journal.json").read_text())
    assert journal["instance_id"] == "ins-1"
    assert journal["status"] == "failed"
    record = json.loads((tmp_path / "native/build-record.json").read_text())
    assert record["provisioner_results"][0]["status"] == "failed"
    assert record["provisioner_results"][0]["failure_category"]


def test_native_resume_skips_only_journaled_provisioners(tmp_path: Path):
    packer = tmp_path / "packer"
    packer.mkdir()
    hcl = ('build { provisioner "shell" { inline = ["echo one"] } '
           'provisioner "shell" { inline = ["echo two"] } }')
    (packer / "main.pkr.hcl").write_text(hcl, encoding="utf-8")
    key = tmp_path / "retained-key"
    key.write_text("private", encoding="utf-8")
    native = tmp_path / "native"
    native.mkdir()
    run_id = "12345678-1234-1234-1234-123456789abc"
    (native / "journal.json").write_text(json.dumps({
        "schema": "https://ohbs-image.dev/native-journal/v1",
        "run_id": run_id, "image_name": "gold-l1", "source_image_id": "img-source",
        "plan_sha256": compile_workspace(tmp_path, _resolved(), "gold-l1").sha256(),
        "instance_id": "ins-retained", "key_id": "skey-retained",
        "key_path": str(key), "completed_provisioners": [1],
    }), encoding="utf-8")
    resolved = _resolved(
        run_id=run_id, secret_id_env="SID", secret_key_env="SKEY",
        security_token_env="TOKEN", ssh_port=22, ssh_username="root", spot=False,
        image_copy_regions=[])
    with (
        mock.patch("ohbs_image._probe_ssh_ready", return_value=True),
        mock.patch("ohbs_image._native._terminate") as terminate,
        mock.patch("ohbs_image._native._teardown_keypair") as teardown,
        mock.patch("ohbs_image._native._native_launch") as launch,
        mock.patch("ohbs_image._native._wait_instance_state",
                   return_value={"InstanceState": "RUNNING", "PublicIpAddresses": ["203.0.113.8"]}),
        mock.patch("ohbs_image._native._scp", return_value=["uploaded"]) as scp,
        mock.patch("ohbs_image._native._ssh", return_value=["two"]),
        mock.patch("ohbs_image._native._create_image", return_value="img-1"),
        mock.patch("ohbs_image._native._copy_images", return_value=[]),
    ):
        result = run_native(tmp_path, resolved, "gold-l1", timeout=600, resume=True)
    assert result.exit_code == 0
    launch.assert_not_called()
    assert scp.call_count == 1
    terminate.assert_called_once_with(resolved, "ins-retained")
    teardown.assert_called_once_with(resolved, "skey-retained", str(key))


def test_temporary_ingress_create_and_exact_delete():
    resolved = _resolved(
        run_id="12345678-1234-1234-1234-123456789abc", family="", ssh_port=22,
        security_group_id="sg-1", secret_id_env="SID", secret_key_env="SKEY",
        security_token_env="TOKEN")
    responses = [
        {"Response": {"SecurityGroupPolicySet": {"Ingress": []}}},
        {"Response": {"RequestId": "create"}},
        {"Response": {"RequestId": "delete"}},
    ]
    with (
        mock.patch("ohbs_image._tc_cloud._my_public_ip", return_value="203.0.113.8"),
        mock.patch("ohbs_image._tc_cloud._creds", return_value=("sid", "key", "")),
        mock.patch("ohbs_image._tc_cloud._tc3_api", side_effect=responses) as api,
    ):
        rule = _create_temporary_ingress(resolved)
        _delete_temporary_ingress(resolved, rule)
    assert rule is not None and rule["CidrBlock"] == "203.0.113.8/32"
    create_params = api.call_args_list[1].args[4]
    delete_params = api.call_args_list[2].args[4]
    assert create_params["SecurityGroupPolicySet"]["Ingress"][0]["PolicyDescription"]
    assert "PolicyDescription" not in delete_params["SecurityGroupPolicySet"]["Ingress"][0]


def test_temporary_ingress_does_not_mutate_when_rule_already_allows_ip():
    resolved = _resolved(
        run_id="12345678-1234-1234-1234-123456789abc", family="", ssh_port=22,
        security_group_id="sg-1", secret_id_env="SID", secret_key_env="SKEY",
        security_token_env="TOKEN")
    policies = {"Ingress": [{"Protocol": "TCP", "Port": "22",
                              "CidrBlock": "203.0.113.8/32", "Action": "ACCEPT"}]}
    with (
        mock.patch("ohbs_image._tc_cloud._my_public_ip", return_value="203.0.113.8"),
        mock.patch("ohbs_image._tc_cloud._creds", return_value=("sid", "key", "")),
        mock.patch("ohbs_image._tc_cloud._tc3_api",
                   return_value={"Response": {"SecurityGroupPolicySet": policies}}) as api,
    ):
        assert _create_temporary_ingress(resolved) is None
    assert api.call_count == 1


def test_native_tencent_source_image_must_be_exact_and_normal():
    resolved = _resolved(source_image_id="img-wanted", region="ap-guangzhou")
    with mock.patch(
        "ohbs_image.native.providers.tencentcloud._tc3_api",
        return_value={"Response": {"ImageSet": [
            {"ImageId": "img-wanted", "ImageState": "CREATING"},
        ], "RequestId": "req-source"}},
    ), pytest.raises(ConfigError, match="is not ready: CREATING"):
        _validate_source_image(resolved, "sid", "skey", "")


def test_native_tencent_source_image_must_match_profile_os_and_architecture():
    resolved = _resolved(source_image_id="img-wanted", region="ap-guangzhou")
    wrong_os = {"Response": {"ImageSet": [{
        "ImageId": "img-wanted", "ImageState": "NORMAL",
        "OsName": "Rocky Linux 9.6", "Architecture": "x86_64",
    }], "RequestId": "req-source"}}
    with (
        mock.patch("ohbs_image.native.providers.tencentcloud._tc3_api",
                   return_value=wrong_os),
        pytest.raises(ConfigError, match="does not match profile tencentos4"),
    ):
        _validate_source_image(resolved, "sid", "skey", "")

    missing_arch = {"Response": {"ImageSet": [{
        "ImageId": "img-wanted", "ImageState": "NORMAL",
        "OsName": "TencentOS Server 4",
    }], "RequestId": "req-source-2"}}
    with (
        mock.patch("ohbs_image.native.providers.tencentcloud._tc3_api",
                   return_value=missing_arch),
        pytest.raises(ConfigError, match="architecture is missing"),
    ):
        _validate_source_image(resolved, "sid", "skey", "")


def test_native_tencent_launch_uses_stable_idempotency_token():
    resolved = _resolved(
        run_id="12345678-1234-1234-1234-123456789abc",
        secret_id_env="SID", secret_key_env="SKEY", security_token_env="TOKEN",
        spot=False, associate_public_ip=True, instance_name="", vpc_id="vpc-1",
        subnet_id="subnet-1", security_group_id="sg-1")
    responses = [
        {"Response": {"ImageSet": [
            {"ImageId": "img-source", "ImageState": "NORMAL",
             "OsName": "TencentOS Server 4", "Architecture": "x86_64"},
        ], "RequestId": "req-source"}},
        {"Response": {"InstanceIdSet": ["ins-1"], "RequestId": "req-launch"}},
    ]
    with (
        mock.patch("ohbs_image.native.providers.tencentcloud._creds",
                   return_value=("sid", "skey", "")),
        mock.patch("ohbs_image.native.providers.tencentcloud._tc3_api",
                   side_effect=responses) as api,
        mock.patch("ohbs_image.native.providers.tencentcloud.time.time", return_value=1_800_000_000),
    ):
        assert launch(resolved, "skey-1", "gold") == "ins-1"
    params = api.call_args_list[1].args[4]
    assert params["ClientToken"] == "ohbs-12345678-1234-1234-1234-123456789abc"
    assert params["ImageId"] == "img-source"
    assert params["InstanceType"] == "S5.MEDIUM2"
    assert params["InstanceChargeType"] == "POSTPAID_BY_HOUR"
    assert params["Placement"] == {"Zone": "ap-guangzhou-4"}
    assert params["VirtualPrivateCloud"] == {"VpcId": "vpc-1", "SubnetId": "subnet-1"}
    assert params["SecurityGroupIds"] == ["sg-1"]
    assert params["LoginSettings"] == {"KeyIds": ["skey-1"]}
    assert params["InstanceCount"] == 1
    assert params["InternetAccessible"]["PublicIpAssigned"] is True
    tags = {item["Key"]: item["Value"]
            for item in params["TagSpecification"][0]["Tags"]}
    assert tags == {
        "managed_by": "ohbs-image", "purpose": "ohbs-image-build",
        "run_id": "12345678-1234-1234-1234-123456789abc",
        "ephemeral": "true", "target_image": "gold",
        "lease_expires_epoch": "1800009000",
    }
    assert resolved._native_provider_evidence["source_image"]["ImageId"] == "img-source"
    assert [item["request_id"] for item in
            resolved._native_provider_evidence["api_requests"]] == [
                "req-source", "req-launch"]
    assert all(item["status"] == "success" for item in
               resolved._native_provider_evidence["api_requests"])
    assert all(item["attempts"] == 1 for item in
               resolved._native_provider_evidence["api_requests"])


def test_native_tencent_call_records_failed_retry_attempts_without_error_text():
    resolved = _resolved()

    def fail_after_retry(*_args, attempt_observer, **_kwargs):
        attempt_observer(1, "retry")
        attempt_observer(2, "network_error")
        raise ConfigError("secret transport detail")

    with (
        mock.patch("ohbs_image.native.providers.tencentcloud._tc3_api",
                   side_effect=fail_after_retry),
        pytest.raises(ConfigError, match="secret transport detail"),
    ):
        _call(resolved, "cvm", "DescribeImages", "2017-03-12",
              "ap-guangzhou", {}, "sid", "skey", None)
    event = resolved._native_provider_evidence["api_requests"][0]
    assert event["status"] == "failed"
    assert event["attempts"] == 2
    assert event["error_type"] == "ConfigError"
    assert "secret transport detail" not in json.dumps(event)


def test_native_api_summary_reports_p95_retries_failures_and_slowest_calls():
    summary = _api_summary([
        {"action": "Fast", "duration_ms": 10, "attempts": 1,
         "status": "success"},
        {"action": "Retry", "duration_ms": 30, "attempts": 3,
         "status": "success"},
        {"action": "Failed", "duration_ms": 20, "attempts": 2,
         "status": "failed", "error_type": "ConfigError"},
    ])
    assert summary == {
        "calls": 3, "failed_calls": 1, "total_attempts": 6, "retries": 3,
        "total_duration_ms": 60.0, "p95_duration_ms": 30.0,
        "max_duration_ms": 30.0,
        "slowest_calls": [
            {"action": "Retry", "duration_ms": 30, "attempts": 3,
             "status": "success"},
            {"action": "Failed", "duration_ms": 20, "attempts": 2,
             "status": "failed", "error_type": "ConfigError"},
            {"action": "Fast", "duration_ms": 10, "attempts": 1,
             "status": "success"},
        ],
    }


def test_native_tencent_wait_instance_fails_fast_on_terminal_state():
    resolved = _resolved(
        secret_id_env="SID", secret_key_env="SKEY", security_token_env="TOKEN")
    response = {"Response": {"InstanceSet": [
        {"InstanceId": "ins-1", "InstanceState": "LAUNCH_FAILED"},
    ], "RequestId": "req-instance"}}
    with (
        mock.patch("ohbs_image.native.providers.tencentcloud._creds",
                   return_value=("sid", "skey", "")),
        mock.patch("ohbs_image.native.providers.tencentcloud._tc3_api",
                   return_value=response),
        pytest.raises(ConfigError, match="terminal state LAUNCH_FAILED"),
    ):
        wait_instance(resolved, "ins-1", "RUNNING", float("inf"))


@pytest.mark.parametrize("state", ["CREATEFAILED", "SYNCING_FAILED"])
def test_native_tencent_image_poll_fails_fast_on_provider_failure_state(state):
    resolved = _resolved()
    response = {"Response": {"ImageSet": [
        {"ImageId": "img-1", "ImageState": state},
    ], "RequestId": "req-image"}}
    with (
        mock.patch("ohbs_image.native.providers.tencentcloud._tc3_api",
                   return_value=response),
        pytest.raises(ConfigError, match=f"failure state {state}"),
    ):
        _wait_image_normal(
            "ap-guangzhou", "img-1", float("inf"), "sid", "skey", "", resolved)


def test_native_tencent_wait_instance_fails_fast_after_spot_disappears():
    resolved = _resolved(
        secret_id_env="SID", secret_key_env="SKEY", security_token_env="TOKEN")
    responses = [
        {"Response": {"InstanceSet": [
            {"InstanceId": "ins-1", "InstanceState": "PENDING"}],
            "RequestId": "req-seen"}},
        {"Response": {"InstanceSet": [], "RequestId": "req-gone"}},
    ]
    with (
        mock.patch("ohbs_image.native.providers.tencentcloud._creds",
                   return_value=("sid", "skey", "")),
        mock.patch("ohbs_image.native.providers.tencentcloud._tc3_api",
                   side_effect=responses),
        mock.patch("ohbs_image.native.providers.tencentcloud.time.sleep"),
        pytest.raises(ConfigError, match="reclaimed or terminated"),
    ):
        wait_instance(resolved, "ins-1", "RUNNING", float("inf"))


def test_native_tencent_copy_verifies_every_region_and_preserves_requested_order():
    resolved = _resolved(
        secret_id_env="SID", secret_key_env="SKEY", security_token_env="TOKEN",
        image_copy_regions=["ap-shanghai", "ap-beijing"])
    resolved._native_provider_evidence = {"api_requests": [], "output_image": {
        "OsName": "TencentOS Server 4", "Architecture": "x86_64",
        "IsSupportCloudinit": True}}
    described_regions: list[str] = []

    def fake_api(_service, action, _version, region, _params, *_credentials,
                 **_options):
        if action == "SyncImages":
            return {"Response": {"ImageSet": [
                {"Region": "ap-beijing", "ImageId": "img-bj"},
                {"Region": "ap-shanghai", "ImageId": "img-sh"},
            ], "RequestId": "req-sync"}}
        described_regions.append(region)
        image_id = "img-sh" if region == "ap-shanghai" else "img-bj"
        return {"Response": {"ImageSet": [
            {"ImageId": image_id, "ImageState": "NORMAL",
             "OsName": "TencentOS Server 4", "Architecture": "x86_64",
             "IsSupportCloudinit": True},
        ], "RequestId": f"req-{region}"}}

    with (
        mock.patch("ohbs_image.native.providers.tencentcloud._creds",
                   return_value=("sid", "skey", "")),
        mock.patch("ohbs_image.native.providers.tencentcloud._tc3_api",
                   side_effect=fake_api),
    ):
        assert copy_images(resolved, "img-source") == ["img-sh", "img-bj"]
    assert sorted(described_regions) == ["ap-beijing", "ap-shanghai"]
    assert [item["region"] for item in
            resolved._native_provider_evidence["replica_images"]] == [
                "ap-beijing", "ap-shanghai"]


def test_native_tencent_create_image_reconciles_lost_response_and_validates_output():
    resolved = _resolved(
        secret_id_env="SID", secret_key_env="SKEY", security_token_env="TOKEN")
    resolved._native_provider_evidence = {
        "api_requests": [],
        "source_image": {"Architecture": "x86_64", "IsSupportCloudinit": True},
    }
    output = {
        "ImageId": "img-output", "ImageName": "gold", "ImageState": "NORMAL",
        "OsName": "TencentOS Server 4", "Architecture": "x86_64",
        "IsSupportCloudinit": True,
    }
    with (
        mock.patch("ohbs_image.native.providers.tencentcloud._creds",
                   return_value=("sid", "skey", "")),
        mock.patch("ohbs_image.native.providers.tencentcloud.wait_instance"),
        mock.patch("ohbs_image.native.providers.tencentcloud._images_by_name",
                   side_effect=[[], [output]]),
        mock.patch("ohbs_image.native.providers.tencentcloud._wait_image_normal",
                   return_value=output),
        mock.patch("ohbs_image.native.providers.tencentcloud._tc3_api",
                   side_effect=[{"Response": {"RequestId": "req-stop"}},
                                ConfigError("network response lost")]) as api,
    ):
        assert create_image(resolved, "ins-1", "gold", float("inf")) == "img-output"
    assert api.call_args_list[1].kwargs["max_retries"] == 1
    assert resolved._native_provider_evidence["create_image_reconciled"] is True
    assert resolved._native_provider_evidence["output_image"]["ImageId"] == "img-output"


def test_native_tencent_output_image_rejects_architecture_drift():
    resolved = _resolved()
    resolved._native_provider_evidence = {
        "api_requests": [], "source_image": {"Architecture": "x86_64"}}
    image = {
        "ImageId": "img-output", "ImageName": "gold", "ImageState": "NORMAL",
        "OsName": "TencentOS Server 4", "Architecture": "arm64",
    }
    with pytest.raises(ConfigError, match="differs from source"):
        _validate_output_image(resolved, image, "gold")


def test_coverage_explanation_distinguishes_failure_manual_scope_and_missing():
    rules = [
        {"id": "1", "assessment": "Automated"},
        {"id": "2", "assessment": "Automated"},
        {"id": "3", "assessment": "Manual"},
        {"id": "4", "assessment": "Automated"},
        {"id": "5", "assessment": "Automated"},
    ]
    results = {
        "1": {"status": "pass", "apply_status": "already"},
        "2": {"status": "pass", "apply_status": "applied"},
        "3": {"status": "manual"},
        "4": {"status": "fail"},
    }
    full = _coverage_explanation(rules, results, scoped=False)
    assert full == {
        "automatic_pass": 1, "remediated_pass": 1, "manual": 1,
        "not_applicable": 0, "environment_limited": 0, "true_failure": 1,
        "pending_reboot": 0, "implementation_missing": 1,
        "not_evaluated_scope": 0,
    }
    scoped = _coverage_explanation(rules, results, scoped=True)
    assert scoped["implementation_missing"] == 0
    assert scoped["not_evaluated_scope"] == 1


def test_coverage_denominator_uses_only_rules_applicable_to_selected_level():
    rules = [
        {"id": "l1", "levels": [1], "platforms": ["Server"]},
        {"id": "l2", "levels": [2], "platforms": ["Server"]},
        {"id": "workstation", "levels": [1], "platforms": ["Workstation"]},
    ]
    assert [item["id"] for item in _rules_for_level(rules, 1)] == ["l1"]
    assert [item["id"] for item in _rules_for_level(rules, 2)] == ["l1", "l2"]


def test_audit_identity_gate_requires_benchmark_level_and_exact_catalog():
    resolved = _resolved(level=1)
    valid = {"benchmark": "CIS-v1.0.0", "profile": "L1",
             "catalog_sha256": "a" * 64}
    assert _audit_identity_failures(valid, resolved, "a" * 64) == []
    invalid = {"benchmark": "CIS-v2.0.0", "profile": "L2",
               "catalog_sha256": "b" * 64}
    assert _audit_identity_failures(invalid, resolved, "a" * 64) == [
        "audit benchmark identity", "audit CIS level identity",
        "audit catalog digest"]


def test_clean_boot_rejects_audit_identity_mismatch_before_score_gate(tmp_path, monkeypatch):
    from argparse import Namespace

    from ohbs_image._commands import cmd_verify_image

    resolved = _resolved(level=1)
    resolved.family = "linux"
    resolved.role_dir = "cis-test"
    resolved.image_benchmark = "CIS-v1.0.0"
    resolved.ssh_port = 22
    resolved.min_score = 85
    resolved.run_id = "12345678-1234-1234-1234-123456789abc"
    catalog = tmp_path / "rules.json"
    catalog.write_text("[]", encoding="utf-8")
    monkeypatch.setattr("ohbs_image._catalog_path", lambda *_: catalog)
    monkeypatch.setattr("ohbs_image._probe_setup_keypair", lambda *_: ("key-1", str(tmp_path / "key"), "pub"))
    monkeypatch.setattr("ohbs_image._probe_launch", lambda *_args, **_kw: "ins-1")
    monkeypatch.setattr("ohbs_image._probe_public_ip", lambda *_: "192.0.2.1")
    monkeypatch.setattr("ohbs_image._probe_ssh_ready", lambda *_args, **_kw: True)
    monkeypatch.setattr("ohbs_image._probe_scan", lambda *_args, **_kw: {
        "benchmark": "CIS-WRONG", "profile": "L1", "catalog_sha256": "0" * 64,
        "summary": {"all": {"score": 100, "fail": 0}}})
    monkeypatch.setattr("ohbs_image._probe_terminate", lambda *_: None)
    monkeypatch.setattr("ohbs_image._probe_teardown_keypair", lambda *_: None)
    monkeypatch.setattr("ohbs_image._write_run_manifest", lambda *_args, **_kw: None)
    assert cmd_verify_image(Namespace(min_score=85), image_id="img-output", resolved=resolved) == 1


def test_native_tencent_termination_is_verified():
    resolved = _resolved(
        secret_id_env="SID", secret_key_env="SKEY", security_token_env="TOKEN")
    responses = [
        {"Response": {"RequestId": "req-terminate"}},
        {"Response": {"InstanceSet": [], "RequestId": "req-describe"}},
    ]
    with (
        mock.patch("ohbs_image.native.providers.tencentcloud._creds",
                   return_value=("sid", "skey", "")),
        mock.patch("ohbs_image.native.providers.tencentcloud._tc3_api",
                   side_effect=responses) as api,
    ):
        terminate(resolved, "ins-1")
    assert [call.args[1] for call in api.call_args_list] == [
        "TerminateInstances", "DescribeInstances"]


def test_native_tencent_key_cleanup_reports_cloud_failure_but_deletes_local_key(tmp_path):
    resolved = _resolved(
        secret_id_env="SID", secret_key_env="SKEY", security_token_env="TOKEN")
    key_dir = tmp_path / "key-material"
    key_dir.mkdir()
    private_key = key_dir / "key"
    private_key.write_text("private", encoding="utf-8")
    failed = {"Response": {"Error": {"Code": "InternalError", "Message": "busy"},
                           "RequestId": "req-delete"}}
    with (
        mock.patch("ohbs_image.native.providers.tencentcloud._creds",
                   return_value=("sid", "skey", "")),
        mock.patch("ohbs_image.native.providers.tencentcloud._tc3_api",
                   return_value=failed),
        pytest.raises(ConfigError, match="req-delete"),
    ):
        teardown_keypair(resolved, "skey-1", str(private_key))
    assert not key_dir.exists()


def test_native_tencent_key_cleanup_retries_eventual_disassociation(tmp_path):
    resolved = _resolved(
        secret_id_env="SID", secret_key_env="SKEY", security_token_env="TOKEN")
    key_dir = tmp_path / "key-material"
    key_dir.mkdir()
    private_key = key_dir / "key"
    private_key.write_text("private", encoding="utf-8")
    associated = ConfigError(
        "DeleteKeyPairs failed: InvalidParameterValue.KeyPairNotSupported "
        "key pair associates with instance")
    with (
        mock.patch("ohbs_image.native.providers.tencentcloud._creds",
                   return_value=("sid", "skey", "")),
        mock.patch("ohbs_image.native.providers.tencentcloud._call",
                   side_effect=[associated, associated, {}]) as call,
        mock.patch("ohbs_image.native.providers.tencentcloud.time.sleep") as sleep,
    ):
        teardown_keypair(resolved, "skey-1", str(private_key))
    assert call.call_count == 3
    assert sleep.call_count == 2
    assert not key_dir.exists()


def test_linux_template_does_not_require_selinux_on_ubuntu_l2():
    assert "case '__OS_TAG__' in ubuntu-*)" in HCL_LINUX_TEMPLATE
    assert "systemctl is-active --quiet ssh ||" in HCL_LINUX_TEMPLATE
