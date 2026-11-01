#!/usr/bin/env python3
"""One command: rerun all 100 smoke companies and print a short smoke report.

    uv run python scripts/smoke_100.py

It runs the evaluator command (`run_competition_batch.py`) on
`smoke/smoke-companies.jsonl`, rebuilds the viewer, checks the run against the
daily budget (45 minutes, 2,000 outbound requests, USD 10), and compares the
published news, jobs and social matches with a previous run. The report is
printed and written to `<out>/SMOKE_REPORT.md` and `<out>/smoke-summary.json`.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRREG_BULK_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"
BUDGET = {"minutes": 45.0, "requests": 2000, "usd": 10.0}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _key(field: str, value) -> str:
    if isinstance(value, dict):
        return str(value.get("url") or value.get("title") or json.dumps(value, sort_keys=True, ensure_ascii=False))
    return str(value)


def published_matches(envelopes: list[dict]) -> dict[str, set[tuple[str, str]]]:
    """(organisation, match) pairs per category, from available claims only."""
    out: dict[str, set[tuple[str, str]]] = {"website": set(), "social": set(), "news": set(), "jobs": set()}
    for envelope in envelopes:
        org = envelope["organisation_number"]
        for claim in envelope.get("claims") or []:
            if claim.get("availability") != "available":
                continue
            field = claim["field"]
            if field == "official_website":
                out["website"].add((org, _key(field, claim["value"])))
            elif field.startswith("company_profile."):
                out["social"].add((org, _key(field, claim["value"])))
            elif field.startswith("public_activity"):
                out["news"].add((org, _key(field, claim["value"])))
            elif field.startswith("hiring"):
                out["jobs"].add((org, _key(field, claim["value"])))
    return out


def withheld_reasons(envelopes: list[dict]) -> dict[str, Counter]:
    out: dict[str, Counter] = {"website": Counter(), "social": Counter(), "news": Counter(), "jobs": Counter()}
    for envelope in envelopes:
        for claim in envelope.get("claims") or []:
            if claim.get("availability") != "ambiguous":
                continue
            field = claim["field"]
            bucket = "website" if field == "official_website" else "social" if field.startswith("company_profile.") else "news" if field.startswith("public_activity") else "jobs" if field.startswith("hiring") else None
            if bucket:
                out[bucket][str(claim.get("reason") or "no reason given")] += 1
    return out


def summarise(envelopes: list[dict], report: dict) -> dict:
    matches = published_matches(envelopes)
    started = datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))
    completed = datetime.fromisoformat(report["completed_at"].replace("Z", "+00:00"))
    minutes = (completed - started).total_seconds() / 60
    requests = int((report.get("operations") or {}).get("requests") or 0)
    cost = sum(float((item.get("operations") or {}).get("third_party_cost_usd") or 0) for item in envelopes)
    confidence = Counter((item.get("confidence") or {}).get("level") or "not_assessed" for item in envelopes)
    states = Counter(claim.get("availability") for item in envelopes for claim in item.get("claims") or [])
    leaked_values = sum(1 for item in envelopes for claim in item.get("claims") or [] if claim.get("availability") != "available" and claim.get("value") is not None)
    return {
        "run_id": report.get("run_id"),
        "envelopes": len(envelopes),
        "expected": report.get("expected_count"),
        "validation_passed": bool((report.get("validation") or {}).get("passed")),
        "minutes": round(minutes, 2),
        "requests": requests,
        "third_party_cost_usd": round(cost, 2),
        "within_budget": minutes <= BUDGET["minutes"] and requests <= BUDGET["requests"] and cost <= BUDGET["usd"],
        "published": {
            "websites": len(matches["website"]),
            "social_profiles": len(matches["social"]),
            "companies_with_news": len({org for org, _ in matches["news"]}),
            "news_items": len(matches["news"]),
            "companies_with_jobs": len({org for org, _ in matches["jobs"]}),
            "job_items": len(matches["jobs"]),
        },
        "withheld": {key: dict(counter.most_common()) for key, counter in withheld_reasons(envelopes).items()},
        "confidence": dict(confidence.most_common()),
        "claim_states": dict(states.most_common()),
        "non_available_claims_with_value": leaked_values,
        "publication_gates": report.get("publication_gates") or {},
    }


def compare(previous: list[dict], current: list[dict]) -> dict:
    before, after = published_matches(previous), published_matches(current)
    return {
        key: {
            "before": len(before[key]),
            "after": len(after[key]),
            "removed": len(before[key] - after[key]),
            "added": len(after[key] - before[key]),
            "removed_examples": sorted(f"{org} {value}" for org, value in before[key] - after[key])[:10],
        }
        for key in before
    }


def render(summary: dict, diff: dict | None, previous_label: str | None, out: Path) -> str:
    p = summary["published"]
    lines = [
        f"# Signalpost smoke report — {summary['run_id']}",
        "",
        f"- Envelopes: **{summary['envelopes']}/{summary['expected']}** · validation **{'passed' if summary['validation_passed'] else 'FAILED'}** · non-available claims carrying a value: {summary['non_available_claims_with_value']}",
        f"- Budget: {summary['minutes']} min of {BUDGET['minutes']:.0f} · {summary['requests']} of {BUDGET['requests']} requests · USD {summary['third_party_cost_usd']:.2f} of {BUDGET['usd']:.0f} → **{'within budget' if summary['within_budget'] else 'OVER BUDGET'}**",
        f"- Published: {p['websites']} websites · {p['social_profiles']} social profiles · news on {p['companies_with_news']} companies ({p['news_items']} items) · jobs on {p['companies_with_jobs']} companies ({p['job_items']} items)",
        "- Confidence: " + " · ".join(f"{level} {count}" for level, count in summary["confidence"].items()),
        "- Claim states: " + " · ".join(f"{state} {count}" for state, count in summary["claim_states"].items()),
        "",
        "## Left blank on purpose (withheld candidates)",
        "",
    ]
    any_withheld = False
    for bucket, reasons in summary["withheld"].items():
        for reason, count in reasons.items():
            any_withheld = True
            lines.append(f"- {bucket}: {count} × {reason}")
    if not any_withheld:
        lines.append("- none")
    if diff is not None:
        lines += ["", f"## Change against {previous_label}", "", "| Matches | Before | After | Removed | Added |", "|---|---:|---:|---:|---:|"]
        for key, row in diff.items():
            lines.append(f"| {key} | {row['before']} | {row['after']} | {row['removed']} | {row['added']} |")
    shown = out.resolve().relative_to(ROOT).as_posix() if out.resolve().is_relative_to(ROOT) else out.name
    lines += ["", f"Viewer: `{shown}/viewer.html` · envelopes: `{shown}/envelopes.jsonl` · run report: `{shown}/report.json`"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Rerun the 100 smoke companies and print a short smoke report")
    parser.add_argument("--companies", default=str(ROOT / "smoke" / "smoke-companies.jsonl"))
    parser.add_argument("--bulk", default=str(ROOT / "data-local" / "brreg-enheter.csv"), help="BRREG entity snapshot; downloaded when missing")
    parser.add_argument("--out", default=str(ROOT / "smoke" / "latest"))
    parser.add_argument("--previous", default=str(ROOT / "smoke" / "v3" / "envelopes.jsonl"), help="Earlier envelopes to compare with; skipped when absent")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--report-only", action="store_true", help="Do not rerun; summarise the envelopes already in --out")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    run_id = "smoke-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if not args.report_only:
        bulk = Path(args.bulk)
        if not bulk.exists():
            bulk.parent.mkdir(parents=True, exist_ok=True)
            print(f"Downloading BRREG snapshot to {bulk} …", file=sys.stderr)
            urllib.request.urlretrieve(BRREG_BULK_URL, bulk)
        command = [
            sys.executable, str(ROOT / "scripts" / "run_competition_batch.py"),
            "--organisations", args.companies, "--bulk", str(bulk),
            "--output", str(out / "envelopes.jsonl"), "--profiles-output", str(out / "profiles.jsonl"),
            "--report", str(out / "report.json"), "--run-id", run_id, "--workers", str(args.workers),
        ]
        result = subprocess.run(command, cwd=ROOT, stdout=subprocess.DEVNULL)
        if result.returncode not in (0, 1):
            raise SystemExit(f"run_competition_batch.py exited with {result.returncode}")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "build_prototype.py"), "--input", str(out / "profiles.jsonl"), "--output", str(out / "viewer.html")], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)

    envelopes = read_jsonl(out / "envelopes.jsonl")
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    summary = summarise(envelopes, report)
    previous_path = Path(args.previous) if args.previous else None
    diff = None
    label = None
    if previous_path and previous_path.exists() and previous_path.resolve() != (out / "envelopes.jsonl").resolve():
        diff = compare(read_jsonl(previous_path), envelopes)
        label = previous_path.relative_to(ROOT).as_posix() if previous_path.is_relative_to(ROOT) else previous_path.as_posix()
        summary["compared_with"] = label
        summary["change"] = diff
    text = render(summary, diff, label, out)
    (out / "SMOKE_REPORT.md").write_text(text, encoding="utf-8")
    (out / "smoke-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(0 if summary["validation_passed"] and summary["within_budget"] else 1)


if __name__ == "__main__":
    main()
