#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import time
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import profile_complete_for_modules, profiles_from_bulk, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.evidence import utc_now  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402
from norway_company_agent.enrichment import discover_website, enrich_from_site, gate_social_links  # noqa: E402
from norway_company_agent.quality import apply_batch_consistency  # noqa: E402
from norway_company_agent.synthesis import build_claims, build_summary  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluator-owned Signalpost batch contract")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", required=True, help="Frozen Brreg entity snapshot")
    parser.add_argument("--output", required=True, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=None, help="Optional guard; defaults to the number of supplied organisation numbers")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--time-budget-seconds", type=float, default=float(os.environ.get("SIGNALPOST_TIME_BUDGET_S", "2400")), help="Wall-clock budget; optional enrichment stops at 60%% of it so every company still returns")
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-discovery", action="store_true", help="Disable legal-name website discovery (baseline behaviour)")
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,website,hiring,public_activity")
    args = parser.parse_args()

    started_at = utc_now()
    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    if args.expected_count is None:
        args.expected_count = len(orgs)
    if len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[profile["organisation_number"]][key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website", "hiring", "public_activity"}
    operations = {"requests": 0, "bytes": 0, "latencies_ms": []}

    soft_deadline = time.monotonic() + 0.6 * args.time_budget_seconds

    def _enrich(profile: dict) -> tuple[dict, dict]:
        records, metrics = fetch_official_modules(profile["organisation_number"], fetch_modules)
        profile["evidence"].update(records)
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "website" in requested_modules:
            website_record, website_metrics = fetch_website(profile.get("website"))
            profile["evidence"]["website"] = apply_website_identity_gate(profile, website_record)["website"]
            gated = profile["evidence"]["website"]
            gate_social_links(profile)
            if time.monotonic() > soft_deadline:
                from norway_company_agent.evidence import evidence as _ev
                note = "optional enrichment skipped to keep the run inside its time budget"
                for key in ("hiring", "public_activity"):
                    if key in requested_modules:
                        profile["evidence"][key] = _ev(key, "source_error", "company_owned_site", "", note=note)
                profile.setdefault("enrichment_trace", {})["skipped"] = note
                return _finish(profile, metrics, website_metrics)
            exact = bool(((gated.get("value") or {}).get("identity_assessment") or {}).get("publishable"))
            if not args.no_discovery and not exact:
                discovered = discover_website(profile, website_metrics, registry_url=gated.get("source_url") if gated.get("status") == "available" else None)
                if discovered:
                    if gated.get("status") == "available":
                        discovered["value"]["registry_listed_candidate"] = {"url": gated.get("source_url"), "identity_assessment": (gated.get("value") or {}).get("identity_assessment")}
                    profile["evidence"]["website"] = discovered
                    gate_social_links(profile)
            if "hiring" in requested_modules or "public_activity" in requested_modules:
                extra = enrich_from_site(profile, profile["evidence"]["website"], website_metrics)
                for key in ("hiring", "public_activity"):
                    if key in requested_modules:
                        profile["evidence"][key] = extra[key]
        return _finish(profile, metrics, website_metrics)

    def _finish(profile: dict, metrics: list, website_metrics: dict) -> tuple[dict, dict]:
        metric = {
            "requests": len(metrics) + website_metrics["requests"],
            "bytes": sum(item.bytes_received for item in metrics) + website_metrics["bytes"],
            "latencies_ms": [item.elapsed_ms for item in metrics] + website_metrics["latencies_ms"],
        }
        profile["run_metrics"] = metric
        return profile, metric

    def enrich(profile: dict) -> tuple[dict, dict]:
        try:
            return _enrich(profile)
        except Exception as exc:  # one broken company must not drop the row
            from norway_company_agent.evidence import evidence as _ev
            for module in requested_modules:
                profile["evidence"].setdefault(module, _ev(module, "source_error", "agent_internal", "", note=f"{type(exc).__name__}: {str(exc)[:200]}"))
            metric = {"requests": 0, "bytes": 0, "latencies_ms": []}
            profile["run_metrics"] = metric
            return profile, metric

    state: dict[str, dict] = {}
    resumed_profiles = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not set(item["organisation_number"] for item in prior).issubset(set(orgs)):
            raise SystemExit("Resume profile membership is not a subset of this batch")
        state = {
            item["organisation_number"]: item
            for item in prior
            if profile_complete_for_modules(item, requested_modules)
        }
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, profile): profile["organisation_number"] for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            profile, metric = future.result()
            state[profile["organisation_number"]] = profile
            operations["requests"] += metric["requests"]
            operations["bytes"] += metric["bytes"]
            operations["latencies_ms"].extend(metric["latencies_ms"])
            if index % args.checkpoint_every == 0 or index == len(pending_profiles):
                checkpoint = [state[org] for org in orgs if org in state]
                write_jsonl(profiles_output, checkpoint)

    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]
    # Cross-company check: a profile or site published for several companies is a group/chain page.
    batch_consistency = apply_batch_consistency(ordered_profiles)
    envelopes = []
    for profile in ordered_profiles:
        envelope = terminal_envelope(profile, run_id=args.run_id, modules=requested_modules, started_at=started_at, completed_at=completed_at)
        try:
            claims, evidence_rows, context = build_claims(profile)
            envelope["claims"] = claims
            envelope["evidence"] = evidence_rows
            envelope["summary"] = build_summary(profile, context, profile.get("changes"))
            envelope["confidence"] = context["confidence"]
            profile["confidence"] = context["confidence"]
            available = [item for item in claims if item["availability"] == "available"]
            envelope["availability"] = "available" if available else "not_available"
        except Exception as exc:  # never drop a row: a broken synthesis is a failed state, not a missing envelope
            envelope["claims"] = []
            envelope["evidence"] = []
            envelope["summary"] = None
            envelope["availability"] = "failed"
            envelope.setdefault("errors", []).append(f"synthesis: {type(exc).__name__}: {str(exc)[:200]}")
        envelope["changes"] = profile.get("changes") or []
        envelope["operations"] = {
            "requests": (profile.get("run_metrics") or {}).get("requests", 0),
            "bytes": (profile.get("run_metrics") or {}).get("bytes", 0),
            "third_party_cost_usd": 0,
        }
        envelopes.append(envelope)
    validation = validate_envelopes(envelopes, args.expected_count)
    write_jsonl(profiles_output, ordered_profiles)
    write_jsonl(Path(args.output), envelopes)
    latencies = sorted(operations.pop("latencies_ms"))
    operations["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    operations["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": args.expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "validation": validation,
        "publication_gates": batch_consistency,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
