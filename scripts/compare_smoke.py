#!/usr/bin/env python3
"""Compare two smoke runs field by field (exact-identity publications only)."""
from __future__ import annotations
import argparse, json
from pathlib import Path

def rows(p):
    return {r["organisation_number"]: r for r in (json.loads(l) for l in Path(p).read_text(encoding="utf-8").splitlines() if l.strip())}

def facts(env):
    ev = env["profile"]["evidence"]
    out = {}
    w = ev.get("website") or {}
    wv = w.get("value") or {}
    pub = w.get("status") == "available" and (wv.get("identity_assessment") or {}).get("publishable")
    out["website"] = 1 if pub else 0
    out["website_discovered"] = 1 if pub and w.get("source_type") == "discovered_company_website" else 0
    out["social_profiles"] = len(wv.get("social_links") or []) if pub else 0
    h = ev.get("hiring") or {}
    hv = h.get("value") or {}
    out["hiring_signal"] = 1 if h.get("status") == "available" else 0
    out["job_items"] = len(hv.get("job_postings") or []) + len(hv.get("applicant_tracking_links") or []) if h.get("status") == "available" else 0
    a = ev.get("public_activity") or {}
    out["activity_items"] = len((a.get("value") or {}).get("items") or []) if a.get("status") == "available" else 0
    out["activity_company"] = 1 if out["activity_items"] else 0
    for k in ("financials", "roles", "locations", "registry_live"):
        out[k] = 1 if (ev.get(k) or {}).get("status") == "available" else 0
    return out

ap = argparse.ArgumentParser(); ap.add_argument("--a", required=True); ap.add_argument("--b", required=True); ap.add_argument("--output")
args = ap.parse_args()
A, B = rows(args.a), rows(args.b)
keys = ["registry_live","financials","roles","locations","website","website_discovered","social_profiles","hiring_signal","job_items","activity_company","activity_items"]
tot = {"a": {k: 0 for k in keys}, "b": {k: 0 for k in keys}}
for org in A:
    fa, fb = facts(A[org]), facts(B[org])
    for k in keys:
        tot["a"][k] += fa.get(k, 0); tot["b"][k] += fb.get(k, 0)
discovered = [{"organisation_number": o, "name": B[o]["profile"].get("name"), "url": (B[o]["profile"]["evidence"]["website"].get("value") or {}).get("final_url"), "proof": ((B[o]["profile"]["evidence"]["website"].get("value") or {}).get("identity_assessment") or {}).get("proof")} for o in B if facts(B[o])["website_discovered"]]
report = {"companies": len(A), "baseline": tot["a"], "candidate": tot["b"], "discovered_websites": discovered}
txt = json.dumps(report, ensure_ascii=False, indent=2)
print(txt)
if args.output: Path(args.output).write_text(txt + "\n", encoding="utf-8")
