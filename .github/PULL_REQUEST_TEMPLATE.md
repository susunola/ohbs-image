## Summary

<!-- One or two sentences: what this PR does and why. Link the issue it
     closes (e.g. "Closes #12") if applicable. -->

## Changes

<!-- Bullet list of the concrete changes. For bug fixes, say what was
     wrong and what the fix does. For new profiles, list the profile
     and its benchmark edition. -->

## Checklist

Before opening, run the same checks CI runs — see
[CONTRIBUTING.md](https://github.com/susunola/ohbs-image/blob/main/CONTRIBUTING.md#before-opening-a-pr):

- [ ] `ruff check ohbs_image`
- [ ] `mypy ohbs_image --ignore-missing-imports`
- [ ] `python3 scripts/check_readme.py --check-tests --check-translations`
- [ ] `python3 scripts/format_rules.py --check`
- [ ] `python3 scripts/check_catalog_guidance.py`
- [ ] `python3 scripts/generate_engines.py && python3 scripts/check_engine_drift.py`
- [ ] `ohbs-image engine verify`
- [ ] `ohbs-image catalog verify`
- [ ] `pytest -v --tb=short`
- [ ] Added a regression test for every bug fix / new flag (mocked at the
      `ohbs_image._tc3_api` boundary where cloud calls are involved)
- [ ] No new runtime (non-dev) dependencies added to `pyproject.toml`
- [ ] No credentials or personal data introduced

## Notes for reviewers

<!-- Anything the reviewer should know: engine payloads re-synced,
     benchmark edition bumped, packaging impact, etc. -->
