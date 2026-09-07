#!/usr/bin/env python3
"""Replay this repo's git history into a table of per-log ingestion ages.

Every commit holds both meta.json (when the scrape happened, and the server's own
clock at that moment) and data.json (each log's ingestedUntil), so one pass over
the log reconstructs the age of every log's ingestion point at every scrape.

Age is measured against the server's `now` when available, so neither scraper
clock skew nor network latency enters the number.

Usage: ./derive_ages.py > ages.csv
"""
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone


def parse_ts(s):
    """Parse RFC 3339. The server's `now` carries nanoseconds; datetime wants at most microseconds."""
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6].ljust(6, '0')}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def blob(sha, path):
    p = subprocess.run(["git", "show", f"{sha}:{path}"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def main():
    shas = subprocess.run(["git", "log", "--format=%H", "--reverse"],
                          capture_output=True, text=True, check=True).stdout.split()

    out = csv.writer(sys.stdout)
    out.writerow(["scraped_at", "reference_time", "log_id", "ingested_until", "age_seconds"])

    rows = skipped = 0
    for sha in shas:
        meta = blob(sha, "meta.json")
        data = blob(sha, "data.json")
        if not meta or not data:
            continue  # bootstrap commits before the first scrape
        if not meta.get("ok"):
            skipped += 1  # failed fetch: data.json is stale, no valid observation here
            continue

        reference = meta.get("server_now") or meta["scraped_at"]
        ref = parse_ts(reference)

        for entry in data.get("logStatus", []):
            age = (ref - parse_ts(entry["ingestedUntil"])).total_seconds()
            out.writerow([meta["scraped_at"], reference, entry["logId"],
                          entry["ingestedUntil"], round(age, 3)])
            rows += 1

    print(f"{rows} observations from {len(shas)} commits "
          f"({skipped} failed scrapes skipped)", file=sys.stderr)


if __name__ == "__main__":
    main()
