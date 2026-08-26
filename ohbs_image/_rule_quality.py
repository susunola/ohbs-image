from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path
from typing import Any

from ._catalog_tools import _iter_catalogs, _load_json
from ._logging import fail, ok, warn
from ._reports import _atomic_write_bytes

RULE_QUALITY_SCHEMA = "https://ohbs-image.dev/rule-quality/v1"
RULE_QUALITY_REPORT_SCHEMA = "https://ohbs-image.dev/rule-quality-report/v1"
_REQUIRED_FIELDS = ("id", "title", "section", "levels", "platforms", "assessment", "family", "params", "risk", "page")
_RISKS = {"safe", "low", "medium", "guarded", "disruptive", "manual", "none"}
_ASSESSMENTS = {"Automated", "Manual"}


def _guidance_map(value: Any) -> dict[str, dict[str, Any]]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items() if isinstance(item, dict)}
    if isinstance(value, list):
        return {
            str(item["id"]): item for item in value
            if isinstance(item, dict) and item.get("id")
        }
    return {}


def assess_rule(rule: dict[str, Any], guidance: dict[str, Any] | None, *, strict: bool) -> dict[str, Any]:
    issues: list[dict[str, str]] = []

    def issue(code: str, message: str, *, semantic: bool = False) -> None:
        issues.append({
            "code": code,
            "severity": "error" if strict or not semantic else "warning",
            "message": message,
        })

    for field in _REQUIRED_FIELDS:
        if field not in rule or rule[field] in (None, "", []):
            issue("missing_field", f"required field {field!r} is missing")
    assessment = str(rule.get("assessment") or "")
    family = str(rule.get("family") or "")
    automated = rule.get("automated")
    risk = str(rule.get("risk") or "")
    if assessment and assessment not in _ASSESSMENTS:
        issue("invalid_assessment", f"unsupported assessment {assessment!r}")
    if risk and risk not in _RISKS:
        issue("invalid_risk", f"unsupported risk {risk!r}")
    if not isinstance(rule.get("levels"), list) or not set(rule.get("levels") or []) <= {1, 2}:
        issue("invalid_levels", "levels must be a non-empty subset of [1, 2]")
    if not isinstance(rule.get("platforms"), list) or not rule.get("platforms"):
        issue("invalid_platforms", "platforms must be a non-empty array")
    if not isinstance(rule.get("params"), dict):
        issue("invalid_params", "params must be an object")
    if assessment == "Automated" and (family == "manual" or automated is False):
        issue("automation_conflict", "Automated rule is implemented as manual or disabled", semantic=True)
    if assessment == "Manual" and automated is True:
        issue("manual_conflict", "Manual rule is marked automated=true", semantic=True)

    guide = guidance or {}
    dimensions = {
        "metadata": all(field in rule and rule[field] not in (None, "", []) for field in _REQUIRED_FIELDS),
        "guidance": bool(guide),
        "rationale": bool(guide.get("rationale")),
        "remediation": bool(guide.get("remediation") or guide.get("remediation_hint")),
        "audit": bool(guide.get("audit")),
        "impact": bool(guide.get("impact")),
        "risk": risk in _RISKS - {"none"},
        "implementation": family not in {"", "manual"} and automated is not False,
        "rollback": bool(rule.get("rollback") or guide.get("rollback")),
    }
    score = round(100 * sum(dimensions.values()) / len(dimensions), 1)
    grade = "A" if score >= 87.5 else "B" if score >= 75 else "C" if score >= 50 else "D"
    priority = ({"disruptive": 40, "guarded": 30, "medium": 20}.get(risk, 10)
                + (20 if assessment == "Automated" else 5)
                + sum(5 for passed in dimensions.values() if not passed)
                + sum(20 for item in issues if item["severity"] == "error"))
    return {
        "schema": RULE_QUALITY_SCHEMA,
        "rule_id": str(rule.get("id") or ""),
        "title": str(rule.get("title") or ""),
        "assessment": assessment,
        "family": family,
        "risk": risk,
        "score": score,
        "grade": grade,
        "priority": priority,
        "dimensions": dimensions,
        "issues": issues,
    }


def assess_catalog(profile: str, path: Path, *, strict: bool = False) -> dict[str, Any]:
    raw_rules = _load_json(path)
    raw_guidance = _load_json(path.parent / "guidance.json")
    if not isinstance(raw_rules, list):
        raise ValueError(f"{profile}: rules.json must contain an array")
    guidance = _guidance_map(raw_guidance)
    rules: list[dict[str, Any]] = [
        assess_rule(rule, guidance.get(str(rule.get("id") or "")), strict=strict)
        if isinstance(rule, dict)
        else {
            "schema": RULE_QUALITY_SCHEMA, "rule_id": "", "title": "",
            "assessment": "", "family": "", "risk": "", "score": 0.0,
            "grade": "D", "priority": 100, "dimensions": {},
            "issues": [{"code": "invalid_rule", "severity": "error", "message": "rule is not an object"}],
        }
        for rule in raw_rules
    ]
    grades = Counter(str(rule["grade"]) for rule in rules)
    dimensions = {
        name: sum(int(bool(rule["dimensions"].get(name))) for rule in rules)
        for name in ("metadata", "guidance", "rationale", "remediation", "audit", "impact", "risk", "implementation", "rollback")
    }
    errors = sum(int(item["severity"] == "error") for rule in rules for item in rule["issues"])
    warnings = sum(int(item["severity"] == "warning") for rule in rules for item in rule["issues"])
    return {
        "profile": profile,
        "rules": len(rules),
        "average_score": round(sum(float(rule["score"]) for rule in rules) / len(rules), 1) if rules else 0.0,
        "grades": dict(sorted(grades.items())),
        "dimensions": dimensions,
        "errors": errors,
        "warnings": warnings,
        "ok": errors == 0,
        "rule_results": rules,
    }


def build_quality_report(*, strict: bool = False, profile: str = "") -> dict[str, Any]:
    catalogs: list[dict[str, Any]] = []
    for item in _iter_catalogs():
        if profile and item["profile"] != profile:
            continue
        catalogs.append(assess_catalog(str(item["profile"]), Path(item["path"]), strict=strict))
    total = sum(int(item["rules"]) for item in catalogs)
    top = sorted(
        ({**rule, "profile": item["profile"]} for item in catalogs for rule in item["rule_results"]),
        key=lambda rule: (-int(rule["priority"]), float(rule["score"]), str(rule["profile"]), str(rule["rule_id"])),
    )[:50]
    return {
        "schema": RULE_QUALITY_REPORT_SCHEMA,
        "strict": strict,
        "ok": all(bool(item["ok"]) for item in catalogs),
        "summary": {
            "profiles": len(catalogs),
            "rules": total,
            "errors": sum(int(item["errors"]) for item in catalogs),
            "warnings": sum(int(item["warnings"]) for item in catalogs),
            "average_score": round(sum(float(item["average_score"]) * int(item["rules"]) for item in catalogs) / total, 1) if total else 0.0,
        },
        "catalogs": catalogs,
        "priority_rules": top,
    }


def render_quality_html(report: dict[str, Any]) -> str:
    summary = report["summary"]
    rows = "".join(
        f"<tr><td>{html.escape(str(item['profile']))}</td><td>{item['rules']}</td>"
        f"<td>{item['average_score']}</td><td>{item['errors']}</td><td>{item['warnings']}</td>"
        f"<td>{html.escape(json.dumps(item['grades'], sort_keys=True))}</td></tr>"
        for item in report["catalogs"]
    )
    priorities = "".join(
        f"<tr><td>{html.escape(str(item['profile']))}</td><td>{html.escape(str(item['rule_id']))}</td>"
        f"<td>{html.escape(str(item['title']))}</td><td>{html.escape(str(item['risk']))}</td>"
        f"<td>{item['score']}</td><td>{html.escape(', '.join(issue['code'] for issue in item['issues']) or 'coverage gaps')}</td></tr>"
        for item in report["priority_rules"]
    )
    return f"""<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>CIS Rule Quality Baseline</title><style>:root{{--ink:#15251e;--muted:#66736c;--line:#d9dedb;--paper:#f4f2eb;--card:#fff;--green:#176b4d;--red:#9f352c}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 system-ui,sans-serif}}main{{max-width:1180px;margin:auto;padding:36px 20px 70px}}header{{background:#173d30;color:#fff;padding:32px;border-radius:16px}}h1{{margin:0 0 8px}}.metrics{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:18px 0}}.metric,section{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px}}.metric b{{display:block;font-size:28px;color:var(--green)}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{font-size:11px;text-transform:uppercase;color:var(--muted)}}section{{margin-top:18px;overflow:auto}}.truth{{border-left:5px solid var(--red)}}footer{{margin-top:22px;color:var(--muted);font-size:12px}}@media(max-width:700px){{.metrics{{grid-template-columns:1fr 1fr}}}}</style><main><header><h1>CIS Rule Quality Baseline</h1><p>结构完整性、语义一致性、说明、审计、修复、风险与回滚覆盖率的真实基线。</p></header><div class=\"metrics\"><div class=\"metric\"><b>{summary['profiles']}</b>profiles</div><div class=\"metric\"><b>{summary['rules']}</b>rules</div><div class=\"metric\"><b>{summary['average_score']}</b>avg score</div><div class=\"metric\"><b>{summary['errors']}</b>errors</div><div class=\"metric\"><b>{summary['warnings']}</b>warnings</div></div><section class=\"truth\"><h2>解释边界</h2><p>评分衡量规则元数据与工程证据完整度，不代表 CIS 认证或安全效果。缺失 audit、impact、rollback 会降低分数；严格模式还会把 Automated/manual 语义冲突判为错误。</p></section><section><h2>Profile baseline</h2><table><thead><tr><th>Profile</th><th>Rules</th><th>Score</th><th>Errors</th><th>Warnings</th><th>Grades</th></tr></thead><tbody>{rows}</tbody></table></section><section><h2>优先治理 50 条</h2><table><thead><tr><th>Profile</th><th>Rule</th><th>Title</th><th>Risk</th><th>Score</th><th>Why</th></tr></thead><tbody>{priorities}</tbody></table></section><footer>Generated by ohbs-image · schema {RULE_QUALITY_REPORT_SCHEMA}</footer></main></html>"""


def cmd_catalog_lint(args: argparse.Namespace) -> int:
    report = build_quality_report(
        strict=bool(args.strict), profile=str(getattr(args, "profile", "") or ""),
    )
    report_path = str(getattr(args, "report", "") or "")
    if report_path:
        target = Path(report_path).expanduser()
        _atomic_write_bytes(target, render_quality_html(report).encode("utf-8"))
        ok(f"Rule quality report saved: {target}")
    if args.output == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["summary"]
        print(f"{summary['profiles']} profiles / {summary['rules']} rules / average {summary['average_score']}")
        for item in report["catalogs"]:
            print(f"{item['profile']:<12} score={item['average_score']:>5} errors={item['errors']:>4} warnings={item['warnings']:>4}")
        if report["ok"]:
            ok("Rule quality lint passed")
        elif args.strict:
            fail("Strict rule quality lint failed; inspect JSON or HTML report")
        else:
            warn("Rule quality gaps found; use --strict to enforce semantic conflicts")
    return 0 if report["ok"] or not args.strict else 1
