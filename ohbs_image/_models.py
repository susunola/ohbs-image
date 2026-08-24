"""Stable domain views for build inputs, release policy, and one run.

``ResolvedConfig`` remains the backwards-compatible facade used by the CLI.
These small immutable views prevent future features from coupling to every
field in that facade and make manifests/fingerprints explicit domain objects.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BuildSpec:
    """Inputs that define the resulting image, safe to serialize or hash."""

    profile_name: str
    region: str
    zone: str
    instance_type: str
    source_image_id: str
    benchmark: str
    level: int
    os_tag: str
    catalog_basename: str


@dataclass(frozen=True)
class ReleasePolicy:
    """Release gates, kept separate from provider/build placement details."""

    min_score: int
    attestation_required: bool
    delivery_report_required: bool
    verify_boot: bool
    allow_scoped_approval: bool


@dataclass(frozen=True)
class RunContext:
    """Ephemeral execution identity, never part of a reusable BuildSpec."""

    run_id: str
    max_build_minutes: int


@dataclass(frozen=True)
class DeliveryReportView:
    """Presentation-neutral catalog coverage totals for delivery reports."""

    total_rules: int
    evaluated_rules: int
    not_evaluated_rules: int
    coverage_percent: int | None
