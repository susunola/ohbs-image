#!/usr/bin/env python3
"""Build a public, self-contained evidence index from portable JSON artifacts."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

INDEX_SCHEMA = "https://ohbs-image.dev/public-evidence-index/v1"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _kind(document: dict[str, Any]) -> str:
    schema = str(document.get("schema") or "")
    if document.get("schemaVersion") == 1 and document.get("runUrl"):
        return "cloud-acceptance"
    for needle, kind in (
        ("production-proof", "production-proof"),
        ("run-slo", "run-slo"),
        ("benchmark", "benchmark"),
        ("release", "release"),
        ("compliance", "compliance"),
    ):
        if needle in schema:
            return kind
    return "other"


def _status(document: dict[str, Any]) -> str:
    value = str(document.get("status") or "").lower()
    if value in {"passed", "success", "verified", "approved", "complete"}:
        return "passed"
    if value in {"failed", "failure", "blocked", "invalid"}:
        return "failed"
    if value in {"incomplete", "partial", "unknown"}:
        return "incomplete"
    if document.get("qualified") is True or document.get("compatible") is True:
        return "passed"
    if document.get("qualified") is False:
        return "incomplete"
    return "available"


def _timestamp(document: dict[str, Any]) -> str:
    for key in ("finishedAt", "generated_at", "recorded_at", "created_at", "timestamp"):
        if document.get(key):
            return str(document[key])
    return ""


def collect(inputs: list[Path]) -> list[dict[str, Any]]:
    """Collect portable JSON without treating malformed files as evidence."""
    paths: set[Path] = set()
    for source in inputs:
        if source.is_file() and source.suffix == ".json":
            paths.add(source)
        elif source.is_dir():
            paths.update(source.rglob("*.json"))
    entries: list[dict[str, Any]] = []
    for path in sorted(paths):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict):
            continue
        entries.append({
            "kind": _kind(document),
            "status": _status(document),
            "profile": str(document.get("profile") or ""),
            "timestamp": _timestamp(document),
            "commit": str(document.get("commit") or document.get("git_commit") or ""),
            "url": str(document.get("runUrl") or document.get("url") or ""),
            "artifact": str(document.get("evidenceArtifact") or path.name),
            "source": str(path),
            "sha256": _digest(path),
            "schema": str(document.get("schema") or document.get("schemaVersion") or ""),
        })
    return sorted(entries, key=lambda row: (row["kind"], row["profile"], row["timestamp"], row["source"]))


def build_document(entries: list[dict[str, Any]], generated_at: str) -> dict[str, Any]:
    counts = {status: sum(row["status"] == status for row in entries)
              for status in ("passed", "failed", "incomplete", "available")}
    return {
        "schema": INDEX_SCHEMA,
        "generated_at": generated_at,
        "summary": {"total": len(entries), **counts},
        "profiles": sorted({row["profile"] for row in entries if row["profile"]}),
        "evidence": entries,
        "limitations": [
            "This index reports only supplied portable artifacts; absence is not success.",
            "Synthetic benchmarks do not replace real-cloud acceptance or elapsed 30/90-day proof windows.",
            "Verify each source SHA-256 and linked workflow or signed release before relying on a claim.",
        ],
    }


def esc(value: Any) -> str:
    """Escape a value for safe interpolation into the evidence index HTML."""
    return html.escape(str(value), quote=True)


def render_html(document: dict[str, Any], title: str) -> str:
    rows = []
    for item in document["evidence"]:
        link = (f'<a href="{esc(item["url"])}">open</a>' if item["url"] else "—")
        rows.append("<tr>" + "".join((
            f'<td><span class="pill {esc(item["status"])}">{esc(item["status"])}</span></td>',
            f'<td>{esc(item["kind"])}</td>', f'<td>{esc(item["profile"] or "—")}</td>',
            f'<td>{esc(item["timestamp"] or "—")}</td>', f'<td><code>{esc(item["commit"][:12] or "—")}</code></td>',
            f'<td>{esc(item["artifact"])}</td>', f'<td><code>{esc(item["sha256"][:16])}…</code></td>',
            f'<td>{link}</td>',
        )) + "</tr>")
    if not rows:
        rows.append('<tr><td colspan="8" class="empty">No evidence supplied. This is intentionally not reported as success.</td></tr>')
    summary = document["summary"]
    limitations = "".join(f"<li>{esc(item)}</li>" for item in document["limitations"])
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><style>:root{{--paper:#f3f1e9;--ink:#17211b;--muted:#607068;--forest:#153b2a;--green:#237552;--amber:#b76a16;--red:#a53c32;--card:#fffef9;--line:#d8d7ce}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}}main{{max-width:1180px;margin:auto;padding:36px 22px 70px}}header{{padding:35px;border-radius:20px;background:var(--forest);color:white}}h1{{margin:0;font-size:clamp(32px,5vw,56px);letter-spacing:-.045em}}header p{{max-width:800px;color:#dbe8e1}}.metrics{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:20px 0}}.metric,.panel{{background:var(--card);border:1px solid var(--line);border-radius:13px}}.metric{{padding:17px}}.metric b{{display:block;font-size:28px;color:var(--forest)}}.metric span{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}}.panel{{padding:22px;margin-top:24px;overflow:auto}}table{{width:100%;min-width:920px;border-collapse:collapse}}th,td{{padding:11px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}th{{font-size:11px;text-transform:uppercase;color:var(--muted)}}.pill{{padding:3px 8px;border-radius:99px;font-size:11px;font-weight:800;background:#e9e6dc}}.pill.passed{{background:#dbeee4;color:#14543a}}.pill.failed{{background:#f3dcd8;color:#812b25}}.pill.incomplete{{background:#f5e4c9;color:#754509}}code{{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}}.empty{{padding:30px;text-align:center;color:var(--muted)}}footer{{margin-top:30px;color:var(--muted);font-size:12px}}a{{color:#176340}}a:focus-visible{{outline:3px solid #e0a241}}@media(max-width:720px){{.metrics{{grid-template-columns:1fr 1fr}}header{{padding:25px}}}}@media print{{body{{background:white}}header{{background:white;color:var(--ink);border:2px solid var(--forest)}}header p{{color:var(--muted)}}.metric,.panel{{break-inside:avoid}}}}</style></head><body><main><header><h1>{esc(title)}</h1><p>Portable, hash-addressed evidence from real workflows and signed releases. Missing or incomplete observation windows remain visible instead of being promoted as assurance.</p></header><section class="metrics"><div class="metric"><b>{summary["total"]}</b><span>Total</span></div><div class="metric"><b>{summary["passed"]}</b><span>Passed</span></div><div class="metric"><b>{summary["failed"]}</b><span>Failed</span></div><div class="metric"><b>{summary["incomplete"]}</b><span>Incomplete</span></div><div class="metric"><b>{len(document["profiles"])}</b><span>Profiles</span></div></section><section class="panel"><h2>Evidence ledger</h2><table><thead><tr><th>Status</th><th>Kind</th><th>Profile</th><th>Observed</th><th>Commit</th><th>Artifact</th><th>SHA-256</th><th>Source</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section><section class="panel"><h2>Interpretation limits</h2><ul>{limitations}</ul></section><footer>Schema: {esc(document["schema"])} · Last updated: {esc(document["generated_at"])}</footer></main></body></html>'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="JSON artifact files or directories")
    parser.add_argument("--output-html", type=Path, default=Path("public-evidence-index.html"))
    parser.add_argument("--output-json", type=Path, default=Path("public-evidence-index.json"))
    parser.add_argument("--title", default="ohbs-image public evidence index")
    parser.add_argument("--generated-at", default="")
    args = parser.parse_args(argv)
    generated = args.generated_at or datetime.now(UTC).isoformat(timespec="seconds")
    document = build_document(collect(args.inputs), generated)
    args.output_json.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.output_html.write_text(render_html(document, args.title), encoding="utf-8")
    print(f"evidence index: {document['summary']['total']} item(s) -> {args.output_html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
