from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from ohbs_image._proof import (
    proof_report,
    record_daily_proof,
    render_proof_html,
    scale_recovery_benchmark,
    verify_ledger,
)


def test_scale_recovery_benchmark_verifies_backup() -> None:
    result = scale_recovery_benchmark(10)
    assert result["search_count"] == 10
    assert result["recovery_verified"] is True


def test_hash_chain_and_incomplete_claim_are_explicit(tmp_path: Path) -> None:
    ledger = tmp_path / "proof.jsonl"
    entry = record_daily_proof(ledger, tmp_path, day=date.today(), benchmark_size=10)
    assert verify_ledger([entry])["valid"] is True
    report = proof_report([entry], window_days=30)
    assert report["production_proof_complete"] is False
    assert "EVIDENCE INCOMPLETE" in render_proof_html(report)


def test_complete_window_requires_real_distinct_days() -> None:
    now = datetime.now(UTC).date()
    rows = []
    previous = ""
    from ohbs_image._registry import _hash
    for offset in range(30):
        row = {"date": (now - timedelta(days=offset)).isoformat(),
               "metrics": {"runs": {"successful": 1, "failed": 0}},
               "scale_recovery": {"recovery_verified": True, "size": 1000},
               "previous_hash": previous}
        row["document_hash"] = _hash(row)
        previous = row["document_hash"]
        rows.append(row)
    assert proof_report(rows, window_days=30)["production_proof_complete"] is True
