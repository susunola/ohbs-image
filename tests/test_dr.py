from ohbs_image._dr import run_dr_drill


def test_all_dr_scenarios_run_in_isolation_and_emit_recovery_evidence():
    report = run_dr_drill()
    assert report["isolated"] is True
    assert report["passed"] is True
    assert {row["scenario"] for row in report["results"]} == {
        "state_database_restore", "worker_lease_recovery",
        "evidence_corruption_detection"}
    assert all(row["rto_seconds"] >= 0 for row in report["results"])
