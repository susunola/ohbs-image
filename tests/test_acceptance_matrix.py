from scripts.acceptance_matrix import PROFILES, build_matrix


def test_rotation_eventually_covers_every_profile_at_both_levels():
    observed = set()
    for week in range(len(PROFILES)):
        observed.update((row["profile"], row["level"])
                        for row in build_matrix("rotation", week=week, max_jobs=2))
    assert observed == {(profile, level) for profile in PROFILES for level in ("1", "2")}


def test_full_matrix_is_hard_capped_and_has_secret_references():
    rows = build_matrix("full", max_jobs=5)
    assert len(rows) == 5
    assert all(row["source_secret"].startswith("TC_MATRIX_") for row in rows)


def test_representative_matrix_covers_linux_and_windows():
    rows = build_matrix("representative", max_jobs=3)
    assert {row["profile"] for row in rows} == {"tencentos3", "ubuntu2404", "win2022"}
