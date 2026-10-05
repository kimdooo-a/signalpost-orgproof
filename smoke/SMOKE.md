# Local smoke tests — 2026-10-05

All runs below were live (real BRREG and company-website requests) from one Windows workstation, Python 3.12.13 via `uv`.
Registry snapshot: BRREG `enheter/lastned/csv` downloaded 2026-10-05, SHA-256 `0b502cd5…4946`, 1,172,017 rows.
Public universe archive SHA-256 verified: `1c89710e…0384` (matches the published value).

## 100-company smoke test (`smoke-companies.jsonl`)

| Run | Command difference | Envelopes | Validation | Requests | Wall time |
|---|---|---:|---|---:|---:|
| `baseline/` | reference behaviour (`--no-discovery`, no hiring/activity modules), 8 workers | 100/100 | passed | 536 | 1 m 41 s |
| `v3/` (this entry) | default command, 16 workers | 100/100 | passed | 831 | 2 m 36 s |

Exact-identity publications, same 100 companies (`compare-baseline-v3.json`):

| Field | Reference | This entry |
|---|---:|---:|
| Official registry, accounts, roles, sub-units | 100 / 100 / 100 / 100 | 100 / 100 / 100 / 100 |
| Verified official website | 2 | **4** (2 found by org-number proof) |
| Company-owned social profiles | 0 | **3** |
| Companies with dated public activity | 0 | **2** (24 dated items) |
| Companies with hiring signals | 0 | 0 |

Claim states in `v3/envelopes.jsonl`: 1,677 available · 485 not_available · 4 ambiguous · 1 blocked; zero non-available claims carry a value.

## 200-company holdout (`holdout-200.jsonl`, seed 1005, no overlap with the smoke set)

Run with the v2 code: same discovery and site-enrichment logic as v3, but 8 workers, longer discovery timeouts, and before the latest-accounts and summary fixes. 200/200 envelopes, validation passed, 1,791 requests, 8 m 39 s.
19 verified websites, of which 5 were found only by org-number proof; 19 company-owned social profiles; 3 companies with hiring signals; 6 companies with dated activity (31 items).
Every org-number-proven publication was checked by hand against its proof span; all spans contain the company's own organisation number.

## What these numbers do not show

The competition's recall score is relative to the pooled, checked findings of all entrants and Builderr's crawlers. A local run cannot measure it.

## Code version note

`v3/` was produced at commit `41ce9ec`. The following commit only lets `iter_bulk` read an uncompressed CSV snapshot as well as the gzip file BRREG serves; for the gzip snapshot used here the code path and output are unchanged.
