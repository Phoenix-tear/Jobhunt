# Sources, terms and attribution

Moved from the former long README. Attribution wording here is what the app shows next to jobs from each source. Behaviour of de-duplication and the job lifecycle is summarised in ARCHITECTURE.md.


All endpoints were checked against the official documentation and probed live on 2026-10-12.
Where reality differed from the docs or from third-party write-ups, the adapter follows reality
and the difference is noted here.

### ATS job boards (phase 2, implemented)

| Source | Endpoint | Auth | Notes |
| --- | --- | --- | --- |
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | none | Whole board in one response; descriptions are entity-encoded HTML (decoded). [Docs](https://docs.greenhouse.io/job-board.html). |
| Lever | `GET api.lever.co/v0/postings/{slug}?mode=json` (EU: `api.eu.lever.co`) | none | Paged with `skip`/`limit`. Unknown region tries US then EU and remembers it. HTML instead of JSON is retried once, then counted as a failure. [Docs](https://github.com/lever/postings-api). |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` | none | Whole board, no pagination. The live payload has a job `id` (the docs omit it) and per-job `compensation`. `isListed:false` jobs are skipped. [Docs](https://developers.ashbyhq.com/docs/public-job-posting-api). |
| Workable | `GET apply.workable.com/api/v1/widget/accounts/{slug}?details=true` | none | The documented `www.workable.com/api/accounts/{slug}` URL 302-redirects here, so the adapter calls the target directly (unknown account = 404). [Help article](https://help.workable.com/hc/en-us/articles/115012771647). |
| SmartRecruiters | `GET api.smartrecruiters.com/v1/companies/{id}/postings?limit=100&offset=N` + detail `.../postings/{postingId}` | none | `limit` is capped at 100. **Unknown company ids return HTTP 200 with `totalFound: 0`**, so an empty board never proves a company exists. The list has no description: the detail call is made only for new jobs that pass the prefilter, at most `detail_fetch_cap_per_company` per run. [Docs](https://developers.smartrecruiters.com/docs/endpoints). |
| Recruitee | `GET {slug}.recruitee.com/api/offers/` | none today | **Per the [official docs](https://docs.recruitee.com/reference/authentication-1), every request needs a per-company `X-Careers-Sites-Token` from 2027-02-10, and only the company itself can create that token.** The adapter works keyless until then; afterwards it reports the source as blocked (HTTP 401) instead of marking slugs dead. Set `sources.recruitee.enabled: false` once it stops working. |

### Aggregators, feeds and curated lists (phase 3, implemented)

Each is behind the same adapter contract (`FeedSource`), toggleable under `sources:`, and stores attribution
text on every sighting (`job_sightings.attribution_text`). Provider limits are enforced in code through a
per-source fetch budget backed by `source_state`; config can lower them, never raise them.

| Source | Endpoint | Limits and terms (as verified 2026-10-12) | Attribution stored |
| --- | --- | --- | --- |
| Remotive | `GET remotive.com/api/remote-jobs?category=...` | At most ~4 fetches/day, 2 requests/minute (enforced: daily cap 4, 31 s host spacing). Jobs are delayed 24 h. Do not republish jobs to third-party sites; links must point back to Remotive. | "Jobs via Remotive (https://remotive.com)..." |
| RemoteOK | `GET remoteok.com/api` | Element 0 is a legal notice (skipped). Must link back with `follow` and credit Remote OK; no logo use. | "Jobs via Remote OK (https://remoteok.com)..." |
| We Work Remotely | `GET weworkremotely.com/categories/remote-programming-jobs.rss` (configurable `feeds:`) | Public RSS, "attribute the links back to We Work Remotely". XML with a DTD is refused. | "Jobs via We Work Remotely..." |
| Himalayas | `GET himalayas.app/jobs/api/search?q=...&page=N` (or cursor browse with `queries: []`) | Free, no key, 20 jobs per page, data refreshes every 24 h (min interval 24 h), 429 on abuse. Visible link and credit when displayed. | "Jobs via Himalayas (https://himalayas.app)..." |
| Jobicy | `GET jobicy.com/api/v2/remote-jobs?industry=...&count=...&cursor=` | Last 7 days only, at most one automated sync per hour (we use 6 h). Keep Jobicy as the source and the canonical `url`. | "Jobs via Jobicy (https://jobicy.com)..." |
| Arbeitnow | `GET www.arbeitnow.com/api/job-board-api?page=N` | "Free public API ... please do not abuse", hourly updates (we poll twice a day); link back appreciated, site terms apply. | "Jobs via Arbeitnow (https://www.arbeitnow.com)..." |
| Adzuna | `GET api.adzuna.com/v1/api/jobs/{country}/search/{page}` | Needs a free key (`.env`). **Official limits: 25/min, 250/day, 1,000/week, 2,500/month** (the "about 1,000 a month" in some write-ups is wrong). Defaults budget 40 calls per run, 100/day, 900/week, 2,200/month and rotate through country x query combinations. Descriptions are truncated, so jobs get `needs_description=1`; Adzuna's redirect URL is kept; predicted salaries are ignored. Personal research use needs acknowledgement of "The Adzuna API". | "Jobs by Adzuna (https://www.adzuna.com); data via The Adzuna API" |
| Hacker News "Who is hiring?" | `hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring` then `/items/{id}` | No key; Algolia's HN API allows 10,000 requests/hour. Heuristic parse of `Company / Role / Location` headers; comments with no recognizable role are skipped, never guessed. Comment text is treated as untrusted data. | "Posted on Hacker News, Ask HN: Who is hiring?..." |
| SimplifyJobs lists | `raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json` and `.../New-Grad-Positions/dev/...` | Read with conditional requests (ETag). The off-season list is the same file as the summer list (filter with `terms:`); coverage is US, Canada and remote. Entries marked inactive close our copy. A warning is logged when the repository's last commit is older than 30 days. **GitHub shows no license for these repositories**: data is read for personal use only and never republished (the database is gitignored). `categories:` defaults to the software and AI/data categories. | "Curated list by SimplifyJobs (https://github.com/...)" |

Notes on what differed from the original brief: the SimplifyJobs path is on branch `dev` (404 on `main`) and
there is no separate off-season file; Adzuna's monthly quota is 2,500; Jobicy and Himalayas use cursor
pagination; the `listings.json` files are ~13 MB with a quarter of entries active.

Feed jobs have no company row (`company_id` is NULL) until an ATS sighting of the same posting merges with
them. `discover --snowball` harvests ATS board slugs from their apply URLs, so a company first seen on an
aggregator or the curated lists is polled directly (full description from the ATS) from then on.

### Job-alert emails (phase 4, implemented)

LinkedIn, Naukri, Internshala, Indeed, Wellfound, Unstop, Foundit, Cutshort, Instahyre and Glassdoor reach
the pipeline only through my own alert emails in you@example.com (Gmail API, scope `gmail.readonly`,
read-only by construction).

- **Parsing** is HTML + string handling (BeautifulSoup, no LLM): job links are recognized by URL shape and
  rewritten to a canonical form (e.g. LinkedIn view links become `linkedin.com/jobs/view/<id>`), then title,
  company and location are read from the surrounding card. Email text is untrusted data: only fields are
  extracted, instruction-like text is never acted on.
- **Never fetched:** links to those platforms are not requested, not even to follow a tracking redirect. A
  link of unknown shape on a platform host is stored verbatim with `needs_description = 1`.
- **ATS exception:** a link that already points at a supported ATS adds the company to discovery
  (`discovered_via = email:<platform>`) and the posting is fetched through the ATS's public API; it then
  merges with the email job by canonical URL and the full description replaces the placeholder.
- **Stored:** parsed fields only, never message bodies. `processed_emails` makes reruns idempotent.
  Jobs are `email_<platform>` sightings; unseen for `email.stale_after_days` they close.
- **Caveat:** the parsers were built against synthetic fixtures modeled on each platform's usual layout
  (real alerts had not arrived yet). Tune with `parse-email` on real `.eml` files, see SETUP.md.

### Never fetched

LinkedIn, Indeed, Naukri, Internshala, Glassdoor, Wellfound and any other site that needs a login or blocks
bots are never requested. They are reachable only through my own job-alert emails (phase 4), and even
there their links are not followed.

### Third-party data

Optional slug lists from [datascry/openroles](https://github.com/datascry/openroles) (`discover --openroles`):
the repository's code is MIT but its dataset (including the tenant lists) is CC BY-SA 4.0. Decision: the lists
are **not** vendored into this repository, because share-alike would then apply to anything redistributed.
They are fetched on demand into the local gitignored database only, `companies.discovered_via` records the
origin, and this line is the attribution: *tenant lists courtesy of datascry/openroles, CC BY-SA 4.0*.

Common Crawl index data is used under the [Common Crawl terms of use](https://commoncrawl.org/terms-of-use).

## How jobs are de-duplicated

A `jobs` row is one real-world posting; `job_sightings` rows record where it was seen.

1. **Same source.** `(source, source_job_id)` is `UNIQUE`. Re-running only refreshes `last_seen_at`
   (and updates the job if its content changed). It never creates a second job.
2. **Across sources.** A *new* sighting is attached to an existing job when either:
   - its **canonical apply URL** equals the job's, *and* the URL points at one posting (a bare
     `company.com/careers` is never used to merge); or
   - its **fingerprint** equals the job's *and* that job has no sighting from the incoming source yet.

   Canonical URL = https, lower-cased host without `www.`, host aliases folded (`job-boards.greenhouse.io`
   to `boards.greenhouse.io`), tracking parameters removed (`utm_*`, `gclid`, `lever-source`, `gh_src`,
   `ref`, ...; job-id parameters such as `gh_jid` are kept), fragment dropped, `/apply`, `/application`,
   `/c/new` suffixes and SmartRecruiters title slugs removed, query sorted.

   Fingerprint = `sha256(company | title | location)` where company drops legal suffixes
   (`Inc`, `LLC`, `GmbH`...), title is lower-cased with bracketed decorations like `(Remote)` removed,
   and location is `<country>:<city>` (remote-only: `remote:<country>`).

Consequences, both tested: the same title in two cities has two fingerprints and does **not** merge; the
same job on Greenhouse and on an aggregator **does** merge. Two postings of one board with the same title
and city stay separate (they are different requisitions). When sightings disagree, the first (primary)
sighting keeps identity fields, later ones only fill gaps, and a complete description always replaces a
truncated one, never the reverse.

## Lifecycle rules

- **Closing.** A successful, valid fetch of an ATS board lists every open job, so a job absent from it
  increments `missed_runs`; at 3 consecutive misses it is closed (`closed_at`, `status='closed'`). A job that
  reappears is reopened with counters reset.
- **Never counted as a miss:** a failed, timed-out, HTML or otherwise invalid fetch, and a *suspicious drop*:
  a board that returns nothing, or under 10% of the jobs held, when at least 10 were open. Such runs are
  recorded in `ingestion_runs.errors_json`; if the board stays that small for 3 consecutive runs it is
  accepted as real and misses start counting. Postings the prefilter rejects still count as "seen".
- **Dead slugs.** 404, redirect-away or an invalid response increments `consecutive_failures`
  (timeouts, 429 and 5xx are transient and do not). After 3 consecutive failures spanning at least 2 UTC days
  the company becomes `inactive`. Inactive companies are retried every 30 days. Any success resets the
  counters and reactivates the company. HTTP 401/403 marks the *source* blocked for the run, not the slug dead.
- **Prefilter.** Applied to titles only; skipped postings are counted in `ingestion_runs` and never stored,
  and `raw_json` is kept only for jobs that pass. Config edits apply retroactively: a held job the current
  prefilter (or a feed's `terms`/`categories` filter) rejects is closed on the next ingest, and reopens if the
  config is loosened again.
- **Feeds.** A feed shows only a window of jobs, so absence from one fetch proves nothing and never counts as
  a miss. A job is closed when (a) the feed marks it inactive (curated lists), or (b) nothing has seen it for
  `stale_after_days` (21-90 depending on the source) after a *successful* fetch. A failed fetch can never
  age jobs out. A job any ATS still lists is never closed by a feed. A `304 Not Modified` refreshes everything
  the feed holds. Minimum interval and daily cap are checked before each run; skipped runs still write an
  `ingestion_runs` row saying why.

