"""Tencent Cloud CVM provider for the cloud-neutral native executor."""
from __future__ import annotations

import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..._config import ResolvedConfig
from ..._logging import ConfigError
from ..._tc_cloud import _creds, _tc3_api
from .registry import ProviderCapabilities, register_provider

SUPPORTED_PACKER_ARGS = frozenset({
    "disk_type", "disk_size", "data_disks", "internet_max_bandwidth_out",
    "project_id",
})

CAPABILITIES = ProviderCapabilities(
    name="tencentcloud", communicators=("ssh",), supports_spot=True,
    supports_image_copy=True, supports_resume=True, supports_assume_role=False)
register_provider(CAPABILITIES, "tencentcloud-cvm", "tencent")

_INSTANCE_FAILURE_STATES = frozenset({
    "LAUNCH_FAILED", "TERMINATED", "TERMINATING", "SHUTDOWN",
})
_IMAGE_FAILURE_STATES = frozenset({"CREATEFAILED", "SYNCING_FAILED"})
_OS_ALIASES = {
    "tencentos": ("tencentos", "tlinux"),
    "rocky": ("rocky",),
    "rhel": ("rhel", "red hat enterprise linux", "redhat"),
    "ubuntu": ("ubuntu",),
}
_KNOWN_ARCHITECTURES = frozenset({"x86_64", "arm64", "aarch64", "i386"})


def _body(response: dict[str, Any], action: str) -> dict[str, Any]:
    """Return a successful Tencent response body with a traceable request ID."""
    body = response.get("Response")
    if not isinstance(body, dict):
        raise ConfigError(f"{action} returned a malformed response")
    error = body.get("Error")
    if error:
        request_id = str(body.get("RequestId") or "unknown")
        raise ConfigError(f"{action} failed (request {request_id}): {error}")
    return body


def _tracked_body(r: ResolvedConfig, response: dict[str, Any],
                  action: str, *, region: str | None = None,
                  duration_ms: float = 0.0, attempts: int = 1,
                  status: str = "success") -> dict[str, Any]:
    body = _body(response, action)
    evidence = getattr(r, "_native_provider_evidence", None)
    if not isinstance(evidence, dict):
        evidence = {"api_requests": []}
        vars(r)["_native_provider_evidence"] = evidence
    requests = evidence.setdefault("api_requests", [])
    requests.append({
        "action": action,
        "region": region or r.region,
        "request_id": str(
            (response.get("Response") or {}).get("RequestId") or ""),
        "duration_ms": round(max(0.0, duration_ms), 3),
        "attempts": max(1, attempts),
        "status": status,
    })
    return body


def _call(r: ResolvedConfig, service: str, action: str, version: str,
          region: str, params: dict[str, Any], sid: str, skey: str,
          token: str | None, *, max_retries: int = 6) -> dict[str, Any]:
    """Call Tencent Cloud and retain secret-free latency/retry evidence."""
    started = time.monotonic()
    attempts = 0

    def observe(number: int, _outcome: str) -> None:
        nonlocal attempts
        attempts = max(attempts, number)

    try:
        response = _tc3_api(
            service, action, version, region, params, sid, skey, token,
            max_retries=max_retries, attempt_observer=observe)
        body = _tracked_body(
            r, response, action, region=region,
            duration_ms=(time.monotonic() - started) * 1000,
            attempts=max(1, attempts))
        return body
    except Exception as exc:
        evidence = getattr(r, "_native_provider_evidence", None)
        if not isinstance(evidence, dict):
            evidence = {"api_requests": []}
            vars(r)["_native_provider_evidence"] = evidence
        response_body = response.get("Response", {}) if "response" in locals() else {}
        evidence.setdefault("api_requests", []).append({
            "action": action,
            "region": region,
            "request_id": str(response_body.get("RequestId") or ""),
            "duration_ms": round(max(0.0, (time.monotonic() - started) * 1000), 3),
            "attempts": max(1, attempts),
            "status": "failed",
            "error_type": type(exc).__name__,
        })
        raise


def _poll_delay(attempt: int) -> float:
    """Poll quickly during normal short transitions, then back off."""
    return min(8.0, 1.0 + attempt)


def _lease_expiry_epoch(r: ResolvedConfig) -> int:
    """Return a bounded orphan-recovery lease for billable build resources."""
    minutes = max(1, int(getattr(r, "max_build_minutes", 90) or 90))
    return int(time.time()) + (minutes + 60) * 60


def _image_identity(image: dict[str, Any]) -> str:
    return " ".join(str(image.get(key) or "") for key in (
        "OsName", "Platform", "ImageName")).strip().lower()


def _validate_image_identity(r: ResolvedConfig, image: dict[str, Any],
                             *, role: str) -> None:
    """Reject an image whose provider facts contradict the selected profile."""
    os_tag = str(r.image_os_tag or "").lower()
    family, separator, version = os_tag.partition("-")
    identity = _image_identity(image)
    aliases = _OS_ALIASES.get(family, (family,))
    if not identity:
        raise ConfigError(f"{role} image OS identity is missing for profile {r.profile_name}")
    if not any(alias and alias in identity for alias in aliases):
        raise ConfigError(
            f"{role} image OS {identity!r} does not match profile {r.profile_name}")
    if separator and version:
        pattern = rf"(?<!\d){re.escape(version)}(?:\.\d+)*(?!\d)"
        if re.search(pattern, identity) is None:
            raise ConfigError(
                f"{role} image OS {identity!r} does not match expected version {version}")
    architecture = str(image.get("Architecture") or "").lower()
    if not architecture:
        raise ConfigError(f"{role} image architecture is missing")
    if architecture not in _KNOWN_ARCHITECTURES:
        raise ConfigError(f"{role} image architecture is unsupported: {architecture}")


def _validate_source_image(r: ResolvedConfig, sid: str, skey: str,
                           token: str | None) -> None:
    images = _call(r, "cvm", "DescribeImages", "2017-03-12", r.region,
                   {"ImageIds": [r.source_image_id]}, sid, skey,
                   token or None).get("ImageSet") or []
    exact = [item for item in images if isinstance(item, dict)
             and str(item.get("ImageId") or "") == r.source_image_id]
    if len(exact) != 1:
        raise ConfigError(
            f"source image {r.source_image_id} was not found uniquely in {r.region}")
    state = str(exact[0].get("ImageState") or "")
    if state != "NORMAL":
        raise ConfigError(f"source image {r.source_image_id} is not ready: {state or 'UNKNOWN'}")
    _validate_image_identity(r, exact[0], role="source")
    evidence = vars(r)["_native_provider_evidence"]
    evidence["source_image"] = {
        key: exact[0].get(key) for key in (
            "ImageId", "ImageName", "ImageState", "OsName", "Platform",
            "Architecture", "CreatedTime", "ImageSource", "IsSupportCloudinit")
    }


def launch(r: ResolvedConfig, key_id: str, image_name: str) -> str:
    sid, skey, token = _creds(r.secret_id_env, r.secret_key_env, r.security_token_env)
    _validate_source_image(r, sid, skey, token)
    params: dict[str, Any] = {
        "ImageId": r.source_image_id, "InstanceType": r.instance_type,
        "InstanceChargeType": "SPOTPAID" if r.spot else "POSTPAID_BY_HOUR",
        "InstanceName": r.instance_name or f"ohbs-native-{r.run_id[:8]}",
        "Placement": {"Zone": r.zone},
        "VirtualPrivateCloud": {"VpcId": r.vpc_id, "SubnetId": r.subnet_id},
        "SecurityGroupIds": [r.security_group_id],
        "InternetAccessible": {
            "PublicIpAssigned": r.associate_public_ip,
            "InternetChargeType": "TRAFFIC_POSTPAID_BY_HOUR",
            "InternetMaxBandwidthOut": 1,
        },
        "LoginSettings": {"KeyIds": [key_id]}, "InstanceCount": 1,
        # Tencent Cloud guarantees RunInstances idempotency for a stable
        # ClientToken. A retry after an ambiguous network failure must not
        # create a second billable build VM.
        "ClientToken": f"ohbs-{r.run_id}"[:64],
        "TagSpecification": [{"ResourceType": "instance", "Tags": [
            {"Key": "managed_by", "Value": "ohbs-image"},
            {"Key": "purpose", "Value": "ohbs-image-build"},
            {"Key": "run_id", "Value": r.run_id},
            {"Key": "ephemeral", "Value": "true"},
            {"Key": "target_image", "Value": image_name},
            {"Key": "lease_expires_epoch", "Value": str(_lease_expiry_epoch(r))},
        ]}],
    }
    extra = r.packer_extra
    unsupported = sorted(set(extra) - SUPPORTED_PACKER_ARGS)
    if unsupported:
        raise ConfigError("native Tencent Cloud provider does not map argument(s): "
                          + ", ".join(unsupported))
    if r.assume_role_arn:
        raise ConfigError("native Tencent Cloud provider does not yet support assume_role_arn")
    system_disk: dict[str, Any] = {}
    if extra.get("disk_type"):
        system_disk["DiskType"] = extra["disk_type"]
    if extra.get("disk_size") is not None:
        system_disk["DiskSize"] = extra["disk_size"]
    if system_disk:
        params["SystemDisk"] = system_disk
    if extra.get("data_disks") is not None:
        params["DataDisks"] = extra["data_disks"]
    if extra.get("internet_max_bandwidth_out") is not None:
        params["InternetAccessible"]["InternetMaxBandwidthOut"] = extra[
            "internet_max_bandwidth_out"]
    if extra.get("project_id") is not None:
        params["Placement"]["ProjectId"] = extra["project_id"]
    body = _call(r, "cvm", "RunInstances", "2017-03-12", r.region,
                 params, sid, skey, token or None)
    ids = body.get("InstanceIdSet") or []
    if not ids:
        raise ConfigError("RunInstances returned no InstanceId")
    return str(ids[0])


def wait_instance(r: ResolvedConfig, instance_id: str, wanted: str,
                  deadline: float) -> dict[str, Any]:
    sid, skey, token = _creds(r.secret_id_env, r.secret_key_env, r.security_token_env)
    attempt = 0
    seen = False
    while time.monotonic() < deadline:
        instances = _call(r, "cvm", "DescribeInstances", "2017-03-12", r.region,
                          {"InstanceIds": [instance_id]}, sid, skey,
                          token or None).get("InstanceSet") or []
        if instances:
            seen = True
            state = str(instances[0].get("InstanceState", ""))
            if state == wanted:
                return dict(instances[0])
            if state in _INSTANCE_FAILURE_STATES:
                raise ConfigError(f"instance {instance_id} entered terminal state {state}")
        elif seen:
            # Once Tencent Cloud has returned the instance, its disappearance
            # is terminal (commonly Spot reclamation), not eventual consistency.
            raise ConfigError(
                f"instance {instance_id} disappeared while waiting for {wanted}; "
                "it may have been reclaimed or terminated")
        time.sleep(_poll_delay(attempt))
        attempt += 1
    raise ConfigError(f"instance {instance_id} did not reach {wanted} before timeout")


def instance_ip(instance: dict[str, Any]) -> str:
    public = instance.get("PublicIpAddresses") or []
    if public:
        return str(public[0])
    for nic in instance.get("NetworkInterfaceSet") or []:
        addresses = nic.get("PublicIpAddresses") or []
        if addresses:
            return str(addresses[0])
    private = instance.get("PrivateIpAddresses") or []
    return str(private[0]) if private else ""


def create_image(r: ResolvedConfig, instance_id: str, image_name: str,
                 deadline: float) -> str:
    sid, skey, token = _creds(r.secret_id_env, r.secret_key_env, r.security_token_env)
    _call(r, "cvm", "StopInstances", "2017-03-12", r.region,
          {"InstanceIds": [instance_id]}, sid, skey, token or None)
    wait_instance(r, instance_id, "STOPPED", deadline)
    existing = {str(item.get("ImageId") or "") for item in
                _images_by_name(r, image_name, sid, skey, token)}
    try:
        body = _call(
            r, "cvm", "CreateImage", "2017-03-12", r.region,
            {"InstanceId": instance_id, "ImageName": image_name,
             "ImageDescription": "Built by OHBS Native Engine", "ForcePoweroff": "TRUE"},
            sid, skey, token or None, max_retries=1)
        image_id = str(body.get("ImageId") or "")
        if not image_id:
            raise ConfigError("CreateImage returned no ImageId")
    except ConfigError as exc:
        candidates = [item for item in _images_by_name(
            r, image_name, sid, skey, token) if str(item.get("ImageId") or "") not in existing]
        if len(candidates) != 1:
            raise ConfigError(
                f"CreateImage outcome is ambiguous and could not be reconciled: {exc}") from exc
        image_id = str(candidates[0]["ImageId"])
        evidence = vars(r)["_native_provider_evidence"]
        evidence["create_image_reconciled"] = True
    image = _wait_image_normal(r.region, image_id, deadline, sid, skey, token, r)
    _validate_output_image(r, image, image_name)
    return image_id


def _images_by_name(r: ResolvedConfig, image_name: str, sid: str,
                    skey: str, token: str | None) -> list[dict[str, Any]]:
    body = _call(
        r, "cvm", "DescribeImages", "2017-03-12", r.region,
        {"Filters": [{"Name": "image-name", "Values": [image_name]}]},
        sid, skey, token or None)
    images = body.get("ImageSet") or []
    return [dict(item) for item in images if isinstance(item, dict)
            and str(item.get("ImageName") or "") == image_name]


def _validate_output_image(r: ResolvedConfig, image: dict[str, Any],
                           image_name: str) -> None:
    _validate_image_identity(r, image, role="output")
    if str(image.get("ImageName") or "") != image_name:
        raise ConfigError("output image name does not match the requested image name")
    evidence = vars(r)["_native_provider_evidence"]
    source = evidence.get("source_image") or {}
    source_arch = str(source.get("Architecture") or "").lower()
    output_arch = str(image.get("Architecture") or "").lower()
    if source_arch and output_arch != source_arch:
        raise ConfigError(
            f"output image architecture {output_arch} differs from source {source_arch}")
    if source.get("IsSupportCloudinit") is True and image.get("IsSupportCloudinit") is False:
        raise ConfigError("output image lost cloud-init support")
    evidence["output_image"] = {
        key: image.get(key) for key in (
            "ImageId", "ImageName", "ImageState", "OsName", "Platform",
            "Architecture", "CreatedTime", "ImageSource", "IsSupportCloudinit")
    }


def _validate_replica_image(r: ResolvedConfig, image: dict[str, Any],
                            region: str) -> dict[str, Any]:
    _validate_image_identity(r, image, role=f"replica {region}")
    source = vars(r)["_native_provider_evidence"].get("output_image") or {}
    for field in ("Architecture", "OsName"):
        expected = str(source.get(field) or "").lower()
        observed = str(image.get(field) or "").lower()
        if expected and observed != expected:
            raise ConfigError(
                f"replica {region} {field} {observed!r} differs from output {expected!r}")
    if source.get("IsSupportCloudinit") is True and image.get("IsSupportCloudinit") is False:
        raise ConfigError(f"replica {region} lost cloud-init support")
    return {"region": region, **{
        key: image.get(key) for key in (
            "ImageId", "ImageName", "ImageState", "OsName", "Platform",
            "Architecture", "CreatedTime", "ImageSource", "IsSupportCloudinit")
    }}


def _wait_image_normal(region: str, image_id: str, deadline: float,
                       sid: str, skey: str, token: str | None,
                       runtime: ResolvedConfig | None = None) -> dict[str, Any]:
    attempt = 0
    while time.monotonic() < deadline:
        if runtime is not None:
            body = _call(runtime, "cvm", "DescribeImages", "2017-03-12", region,
                         {"ImageIds": [image_id]}, sid, skey, token or None)
        else:
            described = _tc3_api("cvm", "DescribeImages", "2017-03-12", region,
                                  {"ImageIds": [image_id]}, sid, skey, token or None)
            body = _body(described, "DescribeImages")
        images = body.get("ImageSet") or []
        exact = [item for item in images if isinstance(item, dict)
                 and str(item.get("ImageId") or "") == image_id]
        if exact:
            state = str(exact[0].get("ImageState", ""))
            if state == "NORMAL":
                return dict(exact[0])
            if state in _IMAGE_FAILURE_STATES:
                raise ConfigError(f"image {image_id} in {region} entered failure state {state}")
        time.sleep(_poll_delay(attempt))
        attempt += 1
    raise ConfigError(f"image {image_id} in {region} did not become NORMAL before timeout")


def copy_images(r: ResolvedConfig, image_id: str,
                deadline: float | None = None) -> list[str]:
    if not r.image_copy_regions:
        return []
    sid, skey, token = _creds(r.secret_id_env, r.secret_key_env, r.security_token_env)
    body = _call(
        r, "cvm", "SyncImages", "2017-03-12", r.region,
        {"ImageIds": [image_id], "DestinationRegions": r.image_copy_regions,
         "ImageSetRequired": True}, sid, skey, token or None)
    by_region = {str(item.get("Region")): str(item.get("ImageId"))
                 for item in body.get("ImageSet") or [] if isinstance(item, dict)
                 and item.get("Region") and item.get("ImageId")}
    missing = [region for region in r.image_copy_regions if region not in by_region]
    if missing:
        raise ConfigError("SyncImages omitted destination region(s): " + ", ".join(missing))

    # SyncImages is asynchronous. Verify all regions concurrently so a returned
    # ID is never reported as a completed image while keeping wall time bounded
    # by the slowest region rather than the sum of all regions.
    deadline = deadline or time.monotonic() + 1800
    errors: list[str] = []
    replicas: list[dict[str, Any]] = []
    workers = min(8, len(r.image_copy_regions))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ohbs-image-sync") as pool:
        futures = {
            pool.submit(_wait_image_normal, region, by_region[region], deadline,
                        sid, skey, token, r): region
            for region in r.image_copy_regions
        }
        for future in as_completed(futures):
            region = futures[future]
            try:
                replicas.append(_validate_replica_image(r, future.result(), region))
            except Exception as exc:
                errors.append(f"{region}: {exc}")
    if errors:
        raise ConfigError("copied image verification failed: " + "; ".join(sorted(errors)))
    vars(r)["_native_provider_evidence"]["replica_images"] = sorted(
        replicas, key=lambda item: str(item["region"]))
    return [by_region[region] for region in r.image_copy_regions]


def terminate(r: ResolvedConfig, instance_id: str) -> None:
    """Terminate a build CVM and verify that it no longer remains active."""
    sid, skey, token = _creds(r.secret_id_env, r.secret_key_env, r.security_token_env)
    _call(r, "cvm", "TerminateInstances", "2017-03-12", r.region,
          {"InstanceIds": [instance_id]}, sid, skey, token or None)
    deadline = time.monotonic() + 120
    attempt = 0
    while time.monotonic() < deadline:
        instances = _call(
            r, "cvm", "DescribeInstances", "2017-03-12", r.region,
            {"InstanceIds": [instance_id]}, sid, skey,
            token or None).get("InstanceSet") or []
        if not instances or str(instances[0].get("InstanceState") or "") == "TERMINATED":
            return
        time.sleep(_poll_delay(attempt))
        attempt += 1
    raise ConfigError(f"instance {instance_id} termination could not be verified")


def teardown_keypair(r: ResolvedConfig, key_id: str, private_key_path: str) -> None:
    """Delete the cloud key and local material, reporting cloud cleanup failure."""
    failure: Exception | None = None
    try:
        if key_id:
            sid, skey, token = _creds(
                r.secret_id_env, r.secret_key_env, r.security_token_env)
            # Tencent Cloud may retain the key-pair association for several
            # minutes after a terminated instance has disappeared.  Image
            # creation makes this window noticeably longer than the normal
            # instance-only path, so keep the bounded cleanup retry alive long
            # enough to cover the observed control-plane lag.
            deadline = time.monotonic() + 600
            attempt = 0
            while True:
                try:
                    _call(r, "cvm", "DeleteKeyPairs", "2017-03-12", r.region,
                          {"KeyIds": [key_id]}, sid, skey, token or None)
                    break
                except ConfigError as exc:
                    # TerminateInstances is verified before this call, but the
                    # key association clears eventually. Retry only that exact
                    # Tencent Cloud state race; other errors remain immediate.
                    associated = ("KeyPairNotSupported" in str(exc)
                                  and "associat" in str(exc).lower())
                    if not associated or time.monotonic() >= deadline:
                        raise
                    time.sleep(_poll_delay(attempt))
                    attempt += 1
    except Exception as exc:
        failure = exc
    finally:
        if private_key_path:
            shutil.rmtree(Path(private_key_path).parent, ignore_errors=True)
    if failure is not None:
        raise failure
