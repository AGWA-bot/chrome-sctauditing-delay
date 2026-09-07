#!/usr/bin/env bash
# Fetch one SCT auditing "known SCTs" response and split it into:
#   data.json - the stable payload (per-log ingestedUntil), canonicalized
#   meta.json - everything that changes on every request (scrape time, server clock)
# Keeping them apart means `git log -p data.json` shows only real changes.
set -uo pipefail

URL='https://sctauditing-pa.googleapis.com/v1/knownscts/length/20/prefix/32A590?key=AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw'

cd "$(dirname "$0")"

scraped_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

http=$(curl -sS --max-time 60 --retry 3 --retry-delay 5 --retry-all-errors \
            -o "$tmp" -w '%{http_code}' "$URL") || http=000

if [ "$http" = "200" ] && jq -e 'type == "object" and has("logStatus")' "$tmp" >/dev/null 2>&1; then
    # Drop hashSuffix (~1 MB of SCT hashes we don't track) and `now` (belongs in meta).
    # Sort logStatus by logId so server-side reordering never shows up as a diff.
    jq -S '{
        responseStatus,
        logStatus: (.logStatus | sort_by(.logId))
    }' "$tmp" > data.json

    jq -S --arg s "$scraped_at" --argjson h "$http" '{
        scraped_at: $s,
        ok: true,
        http: $h,
        server_now: .now,
        response_status: .responseStatus,
        log_count: (.logStatus | length),
        hash_suffix_count: (.hashSuffix | length)
    }' "$tmp" > meta.json
else
    # Record the failed attempt, but leave data.json holding the last good payload.
    jq -n --arg s "$scraped_at" --argjson h "$http" '{
        scraped_at: $s,
        ok: false,
        http: $h
    }' > meta.json
    echo "scrape failed: HTTP $http" >&2
fi
