# chrome-sctauditing-delay

Git-scraping tracker for how far behind Chrome's SCT auditing service is at ingesting
each Certificate Transparency log.

The [SCT auditing](https://source.chromium.org/chromium/chromium/src/+/main:services/network/sct_auditing/)
endpoint that Chrome queries reports, for each CT log it knows about, an `ingestedUntil`
timestamp: the point up to which that log's entries have been absorbed. The metric tracked
here is the **ingestion delay** — how stale that point is at the moment of the scrape:

```
age = now - ingestedUntil        (per logId)
```

## What's stored

Every scrape produces exactly one commit containing two files.

**`data.json`** — the payload, canonicalized. Changes only when the source actually changes,
so `git log -p data.json` is a clean record of ingestion progress.

```json
{
  "responseStatus": "OK",
  "logStatus": [
    { "logId": "ABpdGhwtk3W2SFV4+C9xoa5u7zl9KXyK4xV7yt7hoB4=",
      "ingestedUntil": "2026-09-07T21:22:46.789Z" }
  ]
}
```

**`meta.json`** — everything that differs on every request.

```json
{
  "scraped_at": "2026-09-07T22:35:02Z",
  "ok": true,
  "http": 200,
  "server_now": "2026-09-07T22:35:03.421337881Z",
  "response_status": "OK",
  "log_count": 74,
  "hash_suffix_count": 20576
}
```

Two deliberate choices:

- **Ages are derived, never stored.** Writing an age into `data.json` would make every row
  differ on every run, destroying the diffs that make this worth doing in git at all, and
  it would bake in a subtraction that can't be undone. The raw `ingestedUntil` plus a
  reference clock is strictly more information: it yields the age at any moment, not just
  at the sample points.
- **The response's own `now` goes in `meta.json`, not `data.json`.** It's a property of the
  request, not of the data, and it changes every single time. Splitting it out is what keeps
  `data.json` byte-identical between real updates. Because every commit carries both files,
  the pairing is never lost.

`hashSuffix` (~1 MB, 20k SCT hashes per response) is discarded; only its length is kept in
`meta.json` as a cheap sanity signal.

`meta.json` changes on every run, so there is one commit per scrape even when the data is
unchanged. That heartbeat is what lets you tell "no log advanced for two hours" apart from
"the scraper was broken for two hours". Failed fetches are recorded with `"ok": false` and
leave the last good `data.json` in place.

### Derived artifacts

Rebuilt from the two files above on every scrape by `build.py`:

| file | what it is |
| --- | --- |
| `metrics.txt` | Prometheus text exposition of the current snapshot |
| `logs.json` | append-only log registry; array position is a stable series index |
| `history.jsonl` | one line per scrape, delays aligned to `logs.json`, rolling 14-day window |
| `index.html` | the graph, served from GitHub Pages |

`logs.json` is append-only on purpose: `history.jsonl` rows are positional arrays, so a log
that disappears keeps its slot (later rows just carry `null` there) and indices never shift
under existing history. Log names come from
[Cert Spotter's list](https://loglist.certspotter.org/ALL.json).

**Both that list and Chrome's own split logs across `logs` (RFC 6962) and `tiled_logs`
(static-CT).** Reading only `logs` matches 36 of the 74 here; including `tiled_logs` matches
all 74. This bit us once already — it's the single easiest thing to get wrong.

## Prometheus metrics

`metrics.txt` is regenerated each scrape and served as a static file, so it can be pulled by
a Prometheus `static_configs` scrape, dropped into a node_exporter textfile collector
directory, or just curled.

```
# HELP sct_auditing_ingestion_delay_seconds How far behind the SCT auditing service's ingestion point is for a CT log.
# TYPE sct_auditing_ingestion_delay_seconds gauge
sct_auditing_ingestion_delay_seconds{log_id="1219ENGn9XfCx+lf1wC/+YLJM1pl4dCzAXMXwMjFaXc=",log_name="Google Argon 2026h2",shard_ended="false"} 1616.906
```

| metric | meaning |
| --- | --- |
| `sct_auditing_ingestion_delay_seconds` | `now − ingestedUntil` for one log |
| `sct_auditing_ingested_until_seconds` | the ingestion point itself, as Unix time |
| `sct_auditing_logs_total` | logs in the last successful scrape |
| `sct_auditing_scrape_success` | `1` / `0` for the most recent attempt |
| `sct_auditing_scrape_timestamp_seconds` | when that attempt happened |

The `shard_ended` label is the one to filter on — `shard_ended="true"` marks a log whose
`temporal_interval` has passed, whose delay grows without bound by design. A useful alert
is on the complement:

```promql
max by (log_name) (sct_auditing_ingestion_delay_seconds{shard_ended="false"}) > 86400
```

Samples deliberately carry no inline timestamps, so a scrape reflects when Prometheus read
the file; `sct_auditing_scrape_timestamp_seconds` tells you how stale that read was.

## The graph

`index.html` plots delay per log over the retained window, served from GitHub Pages. It's a
static page — uPlot from a pinned, SRI-checked CDN URL, no build step — that reads
`logs.json` and `history.jsonl` from the same directory and fetches names from Cert Spotter
at load time (that endpoint sends permissive CORS headers, so the browser can read it
directly; if it's unreachable the page falls back to the names committed in `logs.json`).

The y-axis is log-scaled because delays span seconds to months, and ended shards are hidden
by default for the same reason. Clicking a legend row toggles one log, the filter box
narrows by name, and hovering reads every visible series at that instant.

## Deriving the ages

```console
$ ./derive_ages.py > ages.csv
$ head -3 ages.csv
scraped_at,reference_time,log_id,ingested_until,age_seconds
2026-09-07T22:35:02Z,2026-09-07T22:35:03.421337881Z,ABpdGhwtk3W2SFV4+C9xoa5u7zl9KXyK4xV7yt7hoB4=,2026-09-07T21:22:46.789Z,4336.632
```

Ages are measured against `server_now` when present, so neither scraper clock skew nor
network latency lands in the number; `scraped_at` is the fallback. Commits whose scrape
failed are skipped rather than being credited with a stale observation.

For ad-hoc querying, [`git-history`](https://github.com/simonw/git-history) will load the
commit history of `data.json` into SQLite:

```console
$ git-history file logs.db data.json --id logId --convert 'json.loads(content)["logStatus"]'
```

## Reading the numbers

Not every large age is a delay. The 74 logs include retired, read-only, and future shards
whose `ingestedUntil` legitimately sits months in the past or pinned at a final entry. The
signal to watch is the *actively ingesting* set — those whose `ingestedUntil` advances
between commits — where normal delay runs on the order of minutes. Filter on whether a
log's value moved recently before treating its age as a fault.

## Running it

```console
$ ./scrape.sh          # fetch -> data.json + meta.json
$ ./build.py           # -> metrics.txt, logs.json, history.jsonl
```

Scheduled every 15 minutes by `.github/workflows/scrape.yml`, plus a `workflow_dispatch` trigger
for manual runs. Two GitHub Actions caveats worth knowing:

- `schedule` is best-effort. Runs are delayed under load and occasionally dropped entirely,
  so treat the cadence as approximate — `scraped_at` is the truth, not the cron expression.
- Scheduled workflows in public repos are disabled after 60 days without repository
  activity, and *workflow runs are not activity — commits are*. This repo is safe by
  construction: `meta.json` embeds `scraped_at`, so every run commits and the 60-day clock
  never advances. The common "commit only if the data changed" scraper is not safe; it
  silently kills its own schedule during any quiet stretch over 60 days.
  (`simonw/pge-outages` pushes only `github-actions[bot]` commits with the default
  `GITHUB_TOKEN` and has stayed active for years, which confirms bot commits count;
  `simonw/ca-fires-history` kept running but stopped committing in July 2024 and is now
  `disabled_inactivity`.)

The API key in `scrape.sh` is the public Chromium key embedded in the browser's source, not
a credential; it's committed deliberately so the scraper is self-contained.

Observed on the first scrape: of 74 logs, 49 had ingested within the last 24 hours (median
delay ~36 min, p90 ~75 min, fastest ~3 min) and 25 were months stale. The 15-minute cadence
is chosen to sit well below the active set's median so the sampling doesn't alias against
the logs' own ingestion cycles.

Every scrape rewrites `metrics.txt` and `history.jsonl` in full, but git's delta compression
absorbs it: a simulated 192 commits packed to ~2.9 KiB each, or roughly 100 MB/year at this
cadence, with synthetic data that changes every value every scrape (real data churns less).

GitHub Pages is served from the `main` branch root, so each scrape commit republishes the
graph; no separate deploy workflow is involved.
