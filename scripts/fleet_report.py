#!/usr/bin/env python3
"""fleet_report.py — render a fleet delivery report from engine result JSONs.

Usage:  python3 scripts/fleet_report.py <reports-dir> <out-md> [--title TEXT]

Reads every ohbs-image result JSON in <reports-dir> and writes a Markdown
summary (per-OS score table, fail/error details, manual items with
reason hints).  Convert to HTML afterwards with any md->html tool.
"""
from __future__ import annotations

import glob
import json
import os
import sys

MANUAL_REASON = {
    "3.1.1": "site decision: identify whether IPv6 is required",
    "5.1.6": "site-specific sshd access control (AllowUsers/AllowGroups)",
    "6.1.1.2.2": "journal-upload auth needs the site log platform",
    "6.1.1.2.3": "journal-upload needs a site log destination to enable",
    "6.1.2.5": "rsyslog remote log host is site-specific",
    "6.1.2.6": "rsyslog remote log host is site-specific",
    "6.2.1.2.2": "journal-remote auth needs the site log platform",
    "6.2.1.2.3": "journal-upload needs a site log destination to enable",
    "6.2.2.1.2": "journal-upload auth needs the site log platform",
    "6.2.2.1.3": "journal-upload needs a site log destination to enable",
    "6.2.2.6": "rsyslog remote log host is site-specific",
    "6.2.3.6": "rsyslog remote log host is site-specific",
}


def manual_reason(rid: str, title: str) -> str:
    if rid in MANUAL_REASON:
        return MANUAL_REASON[rid]
    t = title.lower()
    if "rename" in t:
        return "site decision: account rename needs the site's chosen name"
    if "domain controller" in t or "netlogon" in t:
        return "domain-controller only (image targets member/standalone)"
    if rid.startswith("19."):
        return "HKCU per-user policy; CIS defines it per user profile"
    if "lockout" in t:
        return "MS-only recommendation; enabling can lock the built-in admin"
    if "logoff" in t:
        return "site decision: force logoff when logon hours expire"
    return "manual control per CIS"


def main() -> None:
    rdir, out_md = sys.argv[1], sys.argv[2]
    title = "ohbs-image CIS hardening fleet report"
    if "--title" in sys.argv:
        title = sys.argv[sys.argv.index("--title") + 1]
    rows = []
    for f in sorted(glob.glob(os.path.join(rdir, "*.json"))):
        with open(f) as fh:
            d = json.load(fh)
        osname = os.path.basename(f).split("-cis")[0]
        s = d.get("summary", {}).get("all", {})
        rows.append((osname, s, d))
    lines = [f"# {title}", "",
             "Scoring: pass / (pass+fail); manual/notapplicable excluded "
             "from the denominator (CIS-CAT convention).", "",
             "## Overview", "",
             "| OS | score | pass | fail | error | manual | total |",
             "|---|---|---|---|---|---|---|"]
    for osname, s, _d in rows:
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            osname, s.get("score"), s.get("pass"), s.get("fail"),
            s.get("error"), s.get("manual"), s.get("total")))
    lines.append("")
    for osname, s, d in rows:
        fails = [r for r in d["results"] if r.get("status") == "fail"]
        errs = [r for r in d["results"] if r.get("status") == "error"]
        mans = [r for r in d["results"] if r.get("status") == "manual"]
        lines.append(f"## {osname} (score {s.get('score')})")
        lines.append("")
        if not fails and not errs:
            lines.append("Failures/errors: none.")
        for r in fails:
            lines.append("- FAIL {} {} — {}".format(
                r["id"], r.get("title", ""), r.get("detail", "")))
        for r in errs:
            lines.append("- ERROR {} {} — {}".format(
                r["id"], r.get("title", ""), r.get("detail", "")))
        lines.append("")
        if mans:
            lines.append("Manual (CIS site-specific/manual controls):")
            for r in mans:
                lines.append("- {} {} — {}".format(
                    r["id"], r.get("title", "").strip(),
                    manual_reason(r["id"], r.get("title", ""))))
        else:
            lines.append("Manual: none.")
        lines.append("")
    with open(out_md, "w") as fh:
        fh.write("\n".join(lines))
    print("wrote", out_md)


if __name__ == "__main__":
    main()
