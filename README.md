# Chrome SCT auditing delay

This repository generates a static website with data recording how far behind
Chrome's SCT auditing service is at ingesting each Certificate Transparency log.

It uses [Chrome's SCT auditing endpoint](https://sctauditing-pa.googleapis.com/v1/knownscts/length/20/prefix/32A590?key=AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw)
as input, scraped every 15 minutes. The API key in `scrape.sh` is the public one
embedded in Chromium's source, not a credential. That endpoint reports an `ingestedUntil`
timestamp per log, plus its own `now`; the delay is the difference between them.
Log names come from [Cert Spotter's ALL.json](https://loglist.certspotter.org/ALL.json)
and log states from
[Chrome's all_logs_list.json](https://www.gstatic.com/ct/log_list/v3/all_logs_list.json).
A log is active when its state is `pending`, `qualified`, `usable` or `readonly`.

It produces as outputs:

* `data.json`, the response with the 1 MB `hashSuffix` array dropped and logs
  sorted by id. It stores the raw `ingestedUntil`; delays are derived, never
  stored, so the file only changes when ingestion actually advances.
* `meta.json`, the per-request fields — scrape time, the server's `now`, HTTP
  status. It changes every run, so every scrape commits even when the data
  did not, which distinguishes "nothing advanced" from "the scraper broke".
* `metrics.txt`, a Prometheus-compatible file, one delay and one ingestion
  point per log, labelled with the log's name, state and whether it is active
* `logs.json`, an append-only log registry, and `history.jsonl`, one row per
  scrape holding delays positionally against it, over a rolling 14 days
* `feed.xml`, an Atom feed of ingestion incidents, and `events.json`, its state.
  A log whose ingestion delay exceeds 24 hours opens an event. A log gets at
  most one event per week; later incidents fold into the open event. Entry
  timestamps never change after publication so feed readers (notably Slack)
  do not repost them. Only active logs are considered, since a retired log's
  delay grows without bound by design.

There's an index.html which renders it with uPlot.

`derive_ages.py` replays the full git history into a CSV, past the 14 days
`history.jsonl` retains. `test_events.py` covers the feed; `test_page.py` drives
index.html in headless Chromium.

GitHub Pages serves the `main` branch root, so each scrape commit republishes
the site; there is no separate deploy workflow.

Before publishing, set `SITE_URL` and `TAG_AUTHORITY` in `build.py`.
`TAG_AUTHORITY` becomes part of every Atom entry id, which is permanent —
changing it later makes every reader repost every entry.
