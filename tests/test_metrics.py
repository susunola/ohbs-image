from __future__ import annotations

import json
from pathlib import Path

from ohbs_image._channels import promote_channel
from ohbs_image._distribution import record_replica
from ohbs_image._metrics import collect_metrics, otlp_metrics, prometheus_metrics
from ohbs_image._registry import register_release


def _artifact(tmp_path):
    release = tmp_path / "releases" / "img-1.json"
    release.parent.mkdir(parents=True)
    release.write_text(json.dumps({"image_id": "img-1", "run_id": "run-1",
                                   "profile": "rhel10", "region": "ap-guangzhou",
                                   "state": "approved"}), encoding="utf-8")
    register_release(release, tmp_path / "registry")


def test_metrics_cover_registry_channels_and_replicas(tmp_path):
    _artifact(tmp_path)
    root = tmp_path / "registry"
    promote_channel("rhel10", "stable", "img-1", root=root)
    record_replica("img-1", "ap-shanghai", "img-copy", root=root)
    snapshot = collect_metrics(tmp_path)
    assert snapshot["artifacts"] == {"active": 1}
    assert snapshot["channels"] == 1
    assert snapshot["replicas"] == {"ready": 1}


def test_prometheus_output_has_types_labels_and_terminal_newline(tmp_path):
    _artifact(tmp_path)
    output = prometheus_metrics(collect_metrics(tmp_path))
    assert "# TYPE ohbs_image_runs_total gauge" in output
    assert 'ohbs_image_artifacts{status="active"} 1' in output
    assert "ohbs_image_run_success_ratio 0" in output
    assert output.endswith("\n")


def test_otlp_json_has_service_resource_and_metrics(tmp_path):
    _artifact(tmp_path)
    resource = otlp_metrics(collect_metrics(tmp_path))["resourceMetrics"][0]
    assert resource["resource"]["attributes"][0]["value"]["stringValue"] == "ohbs-image"
    names = {metric["name"] for metric in resource["scopeMetrics"][0]["metrics"]}
    assert {"ohbs.image.run.success_ratio", "ohbs.image.artifacts"} <= names


def test_prometheus_alert_rules_are_packaged():
    content = Path("integrations/prometheus/ohbs-image-alerts.yml").read_text(encoding="utf-8")
    assert "OhbsImageLowBuildSuccessRate" in content
    assert "OhbsImageReplicaStuck" in content
