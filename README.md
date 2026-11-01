# Signalpost entry — deterministic, zero-cost company research agent

Built on the Builderr Signalpost reference agent (the published starter kit, unchanged where not noted) with three additions:

1. **Org-number-proven website discovery.** When the registry has no usable website, the agent derives up to four domain candidates from the legal name (`<name>.no`, hyphenated, without "og", `.com`), drops candidates that do not resolve in DNS (no HTTP request), respects `robots.txt`, and publishes a domain **only if the exact 9-digit organisation number is printed on its homepage or on a same-domain contact/about page**. Pages that print two or more other organisation numbers (directories, accountants' client lists, portfolios) are rejected. A registry-listed site that fails the name check gets the same org-number check before it is abstained.
2. **Hiring and dated public activity from the verified site only:** careers pages, schema.org `JobPosting`, links to Norwegian applicant-tracking systems (Webcruiter, Jobbnorge, Teamtailor, ReCman, HR-manager, Easycruit, Finn job ads, NAV ads …), RSS/Atom feeds advertised by the site, schema.org `Article`, and `<article><time datetime>` entries.
3. **Claim-level envelope and a source-bounded summary.** Each envelope now carries `claims[]` (one of `available / not_available / blocked / not_applicable / ambiguous / failed`, never zero-for-missing), `evidence[]` (source URL, retrieval time, reporting period, content SHA-256, extraction method), and a template `summary` whose every sentence cites evidence ids and whose `unknowns` lists what was not found.

4. **Publication gates — a wrong match is worse than a blank** (`src/norway_company_agent/quality.py`, added after reviewer feedback on 1 November 2026). Uncertain news, jobs and social matches are left blank with a one-line reason (`availability: "ambiguous"`, `value: null`, `reason`), never published:
   - **News** keeps only dated articles on the company's own site. Withheld: default CMS posts ("Hello world!", "Sample Page"), static pages (`/om-oss`, `/tjenester`, …), items whose only URL is the homepage or the listing page, comment feeds, off-site links, future or implausible dates. Items read off a homepage or listing page are opened and kept only if the article page itself prints a publication date (`article:published_time`, JSON-LD Article/BlogPosting `datePublished`, or `<article><time>`); otherwise the reason is "page has no date".
   - **Jobs** keeps only the company's own careers page or real postings. Withheld: login-only applicant-system dashboards (`app.teamtailor.com`, `/login`, `/users/sign_in` …), vendor homepages and "powered by" links (`teamtailor.com`, `webcruiter.no/` …), careers links that redirect to the homepage or show a password field, pages that do not mention jobs, and postings whose `validThrough` has passed.
   - **Social profiles** are published only when the handle names this exact legal entity. Withheld: handles that carry only part of the legal name (the shared group/brand), handles naming a group (`group`, `gruppen`, `konsern`, `holding` …), handles naming another company in the official BRREG group structure (`konsernstruktur`), and any profile or website that the same run published for more than one company.
   - **Confidence per company** (`envelope.confidence` and `confidence` on every available claim): the weakest published claim decides the level. Registry facts anchored on the organisation number: 0.99. Website proven by the organisation number printed on it: 0.97 (high). Website matched on the exact legal name only: 0.85 (medium); site-derived news, jobs and profiles inherit ×0.95. `confidence.sections` gives website / social / news / jobs as published, withheld (with reason) or blank (with reason). The viewer shows the level on every company, a confidence filter, and the reason for every blank.

Safety: one company that throws is returned as a `source_error` row instead of dropping the batch; optional enrichment stops at 60 % of `--time-budget-seconds` (default 2400, or `SIGNALPOST_TIME_BUDGET_S`) so every company still returns.

## One command (evaluator)

```bash
uv sync --frozen && uv run python scripts/run_competition_batch.py   --organisations <BATCH.jsonl> --bulk <BRREG_ENHETER_SNAPSHOT.csv(.gz)>   --output out/envelopes.jsonl --profiles-output out/profiles.jsonl   --report out/run-report.json --run-id <RUN_ID>
```

`--expected-count` is optional and defaults to the number of supplied organisation numbers. `--no-discovery` reproduces the reference behaviour for A/B comparison.

## Declared models, APIs, licences, cost

| Item | Value |
|---|---|
| Models / LLMs | **None.** No model calls of any kind. |
| APIs | Brønnøysundregistrene Enhetsregisteret (entity, roles, sub-units, group) and Regnskapsregisteret open APIs — NLOD 2.0, no key. Company-owned websites (robots.txt respected). |
| Paid third-party APIs | None. No search API, no scraping of LinkedIn/Meta/Glassdoor/Indeed/Google. |
| Secrets | None required. |
| Expected cost per official run | **USD 0.00** |

## 100-company smoke test — one command

```bash
uv sync --frozen && uv run python scripts/smoke_100.py
```

Reruns all 100 companies in `smoke/smoke-companies.jsonl` with the evaluator command (downloads the BRREG snapshot to `data-local/` if it is missing), rebuilds the viewer, checks the run against the daily budget (45 minutes, 2,000 requests, USD 10), and prints a short report: validation, runtime, requests, cost, published websites / social profiles / news / jobs, withheld candidates by reason, confidence levels, and the change against an earlier run (`--previous`, default `smoke/v3/envelopes.jsonl`). Output: `smoke/latest/SMOKE_REPORT.md`, `smoke-summary.json`, `envelopes.jsonl`, `report.json`, `viewer.html`.


`smoke/` holds the local smoke test (`smoke/smoke-companies.jsonl`, selected with `select_entry_batch.py` default seed): `smoke/v3/report.json` (machine-readable run report), `smoke/v3/envelopes.jsonl` (100 terminal envelopes), `smoke/v3/viewer.html` (desktop/mobile viewer), and `smoke/compare-baseline-v3.json` (same batch, reference behaviour vs. this entry). See `smoke/SMOKE.md`.

---

# Signalpost reference agent

This is a runnable starting point for the Signalpost company-research challenge. It is intentionally a solid baseline, not a winning submission.

The public universe contains 411,160 eligible companies. Run the starter on 100 companies before submitting. Larger local tests, including 1,000 or more companies, are encouraged but their precomputed profiles are not submitted or scored.

## What it already does

- reads a batch of Norwegian organisation numbers;
- anchors identity in the Brønnøysund bulk registry;
- fetches official financials, roles, group links and registered workplaces;
- visits the registry-listed website and rejects weak entity matches;
- emits one terminal JSONL envelope per input;
- records sources, retrieval times, content hashes, request counts and latency;
- supports checkpoint/resume and a deterministic refresh replay;
- includes examples for external-footprint discovery and an evidence-bounded research agent.

## First run: try one saved example

Requires Python 3.12+. Open a terminal inside this extracted folder.

Before downloading company data or running a full crawl, try the bundled public
sample. It uses saved responses: no API key, registry download or live web requests.

```bash
python3 scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json \
  --output out/refresh-demo.json
```

Open `out/refresh-demo.json`. The `events` list shows what changed between two
versions of one company profile and the source evidence for each change. The sample
should find two expected changes, no false changes, and no extra changes when the
same data is checked again.

The report's `qualification_passed` field refers only to this public sample check.
It does not qualify an entry for the competition or prove live information coverage.
The printed request counts are reads from saved responses, not network calls.

## Next: research live companies

Requires Python 3.12+ and `uv`. This step downloads data and makes live requests.
The manifest selector can create a local test batch of any size. Use 100 rows for the recommended smoke test before trying a larger batch.

```bash
uv sync
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv
curl -L 'https://builderr.ai/signalpost-company-universe-2025.jsonl.gz' -o signalpost-universe.jsonl.gz

uv run python select_entry_batch.py \
  --universe signalpost-universe.jsonl.gz \
  --count 100 \
  --output entry-companies.jsonl

# Use the 100-company batch as your smoke test.
cp entry-companies.jsonl smoke-companies.jsonl

uv run python scripts/run_competition_batch.py \
  --organisations smoke-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/smoke-profiles.jsonl \
  --output out/smoke-envelopes.jsonl \
  --report out/smoke-report.json \
  --run-id smoke-001 \
  --expected-count 100

# You may test at larger scale locally, but Builderr supplies the official batch for scoring.
uv run python scripts/run_competition_batch.py \
  --organisations entry-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/profiles.jsonl \
  --output out/envelopes.jsonl \
  --report out/run-report.json \
  --run-id local-001 \
  --expected-count 1000

uv run --with pytest pytest -q
```

The published archive was clean-room verified on August 24, 2026: 104 tests and 5 subtests passed, followed by a one-company live BRREG smoke run with one terminal envelope, five requests and zero silent drops.

Increase `--count` and `--expected-count` together for a larger local test. The 100-row smoke test above is practice only; Builderr supplies the companies for every official run.

## The improvement loop

1. Treat the organisation number as the anchor.
2. Generate site/profile candidates from official data, the company site, lawful search providers and named people.
3. Save every candidate and the evidence for or against it.
4. Publish only exact-entity matches. Parent, brand, franchise and similarly named companies are not exact.
5. Crawl static HTML first. Escalate to a browser only when a deterministic completeness check fails.
6. Measure added supported coverage, wrong-company claims, runtime, requests and cost.
7. Promote a strategy only when it improves coverage without weakening the accuracy gates.
8. Freeze strategies and thresholds before the daily evaluation run.

The strongest differentiator is external evidence that remains exact and auditable: official company pages, company-owned profiles, jobs, dated activity, ratings/reviews and permitted public signals. Do not trade accuracy for volume.

## Important source rule

Open-source code does not grant permission to scrape a platform. Follow each source's terms, robots policy, rate limits and licence. LinkedIn, Meta and Indeed are useful identity/discovery targets, but direct automated collection may be restricted. Use permitted APIs, licensed providers, company-owned outbound links, or return `blocked`/`not_available`.

Read `docs/competition-control-loop.md`, `docs/external-connectors.md` and the public source policy before adding connectors.

## Submission contract

Submit a repository with:

- a 100-company smoke-test result or report;
- one documented command that accepts a JSONL batch of organisation numbers;
- exactly one terminal envelope per input;
- pinned dependencies and reproducible setup;
- a previous-snapshot input and material-change output;
- a machine-readable run report with runtime, request count and third-party cost;
- declared models, APIs, licences and source-rights assumptions.

Email the repository URL, run command, models/APIs and expected cost per 100-company run to `submit@builderr.ai`.
