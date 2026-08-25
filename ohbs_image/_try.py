"""``ohbs-image try`` — zero-cost, offline demo of the hardened-image pipeline.

No Tencent Cloud account, no CVM, no spend: the command runs the same
bundled engine + catalog gates that CI runs, then renders a sample
single-page HTML compliance report for the chosen profile, so an evaluator
sees the actual deliverable within a minute.  It also runs inside the
shipped container image (``docker build --target try``), giving the demo a
clean, reproducible environment.

What the demo deliberately does NOT do: touch the cloud, spend money, or
produce an image.  A real ``ohbs-image build`` adds the ephemeral build VM,
remediation, clean-boot verification, SLSA provenance signing, SBOM and
delivery evidence — the demo shows the engine, the catalogs and the report,
which are the same bytes a real build ships.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from ._catalog import _catalog_basename
from ._catalog_tools import cmd_catalog_verify
from ._engine import cmd_engine_verify
from ._logging import banner, fail, info, ok
from ._profiles import PROFILES, SAMPLE_CONFIG
from ._reports import _load_report_catalog, _ReportContext, _write_build_html_report


def _demo_status(rule_id: str) -> str:
    """Deterministic pseudo-audit status derived from the rule ID.

    The demo report must be reproducible across machines, so statuses are a
    pure function of the rule ID (no RNG): roughly 86% pass, 10% manual
    review, 4% fail — a realistic-looking but clearly synthetic result set.
    """
    digits = re.sub(r"[^0-9]", "", str(rule_id))
    if not digits:
        return "pass"
    mod = sum(int(ch) for ch in digits) % 100
    if mod < 4:
        return "fail"
    if mod < 14:
        return "manual"
    return "pass"


def cmd_try(args: argparse.Namespace) -> int:
    """Run the zero-cost demo: engine gates + sample catalog + HTML report."""
    profile = args.profile
    meta = PROFILES.get(profile)
    if not isinstance(meta, dict) or not meta.get("role_dir"):
        fail(f"unknown profile {profile!r}; pick one of: "
             + ", ".join(sorted(PROFILES)))
        return 1
    out_dir = Path(args.output).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    banner("try")
    info(f"Zero-cost demo for {profile} L{args.level} — no cloud access, no spend")
    info(f"Output directory: {out_dir}")

    # 1) The same bundled-engine and catalog gates CI runs (offline).
    if cmd_engine_verify(argparse.Namespace(output="text")) != 0:
        fail("bundled engine gate failed — the package may be damaged")
        return 1
    if cmd_catalog_verify(argparse.Namespace(output="text", strict=False)) != 0:
        fail("bundled catalog gate failed — the package may be damaged")
        return 1

    # 2) A ready-to-edit starter config (the same one `configure` uses).
    config_path = out_dir / "ohbs-image.toml"
    config_path.write_text(SAMPLE_CONFIG, encoding="utf-8")
    info(f"Starter config -> {config_path}")

    # 3) Sample audit derived deterministically from the profile's own
    #    bundled rule catalog + guidance (the real deliverable's data).
    role_dir = str(meta["role_dir"])
    benchmark = str(meta.get("benchmark") or "CIS")
    ctx = _ReportContext(
        profile_name=profile,
        level=int(getattr(args, "level", 1)),
        region="demo-region",
        zone="demo-zone",
        source_image_id="img-demo-source",
        image_benchmark=benchmark,
        run_id="try-demo",
        role_dir=role_dir,
    )
    catalog, guidance = _load_report_catalog(ctx)
    if not catalog:
        fail(f"no bundled catalog for {profile} ({_catalog_basename(role_dir, benchmark)})")
        return 1
    results: list[dict[str, Any]] = []
    counts = {"pass": 0, "fail": 0, "manual": 0, "error": 0}
    for rule in catalog:
        rule_id = str(rule.get("id", ""))
        status = _demo_status(rule_id)
        counts[status] += 1
        results.append({
            "id": rule_id,
            "title": rule.get("title", ""),
            "section": rule.get("section", ""),
            "levels": rule.get("levels", []),
            "assessment": rule.get("assessment", "Automated"),
            "status": status,
            "apply_status": ("applied" if status == "pass"
                             else "skipped_manual" if status == "manual"
                             else "apply_failed"),
        })
    evaluated = counts["pass"] + counts["fail"]
    score = round(100 * counts["pass"] / evaluated, 1) if evaluated else None
    audit_doc = {
        "mode": "demo",
        "benchmark": benchmark,
        "summary": {"all": counts},
        "results": results,
    }
    audit_path = out_dir / "demo-audit.json"
    audit_path.write_text(json.dumps(audit_doc, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    info(f"Sample audit JSON -> {audit_path}")

    # 4) The actual single-page HTML delivery report, rendered by the same
    #    code path a real build uses.
    report_path = out_dir / "demo-report.html"
    written = _write_build_html_report(
        ctx, ["img-demo-0001"], f"demo-{profile}", score, audit_path,
        provenance=None, signed=False, dest=report_path)
    if written is None:
        fail("could not write the demo HTML report")
        return 1
    ok(f"Sample HTML compliance report -> {written}")
    print()
    ok(f"Demo complete: {len(catalog)} rules, score {score:g}% (synthetic)")
    print("What a real `ohbs-image build` adds on top: an ephemeral CVM, "
          "remediation, clean-boot verification, SLSA provenance signing, "
          "SBOM and release manifests.")
    print(f"Next step: edit {config_path} (region/zone/image IDs) and run "
          "`ohbs-image preflight` then `ohbs-image build`.")
    return 0
