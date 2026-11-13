# Signalpost smoke report — smoke-20261113T215929Z

- Envelopes: **200/200** · validation **passed** · non-available claims carrying a value: 0
- Budget: 4.77 min of 45 · 1782 of 2000 requests · USD 0.00 of 10 → **within budget**
- Published: 23 websites · 21 social profiles · news on 8 companies (62 items) · jobs on 3 companies (4 items)
- Confidence: high 191 · medium 9
- Claim states: available 3471 · not_available 879 · ambiguous 40 · failed 6 · blocked 1

## Left blank on purpose (withheld candidates)

- website: 8 × site does not carry the legal name or the organisation number
- website: 6 × site prints a different organisation number as its own
- website: 1 × non-Norwegian domain (.net) without the organisation number or registry address
- website: 1 × non-Norwegian domain (.com) without the organisation number or registry address
- social: 12 × handle does not carry the legal name
- news: 5 × article page could not be opened (HTTPError)
- news: 2 × item points at the homepage, not an article
- news: 2 × date belongs to a listing or static page, not an article
- news: 2 × page has no date
- jobs: 1 × posting closed on 2026-03-06

## Change against holdout run at cdece8d (2026-11-14, same day)

| Matches | Before | After | Removed | Added |
|---|---:|---:|---:|---:|
| website | 18 | 23 | 1 | 6 |
| social | 19 | 21 | 3 | 5 |
| news | 27 | 62 | 0 | 35 |
| jobs | 2 | 4 | 0 | 2 |

Viewer: `smoke/holdout-latest/viewer.html` · envelopes: `smoke/holdout-latest/envelopes.jsonl` · run report: `smoke/holdout-latest/report.json`
