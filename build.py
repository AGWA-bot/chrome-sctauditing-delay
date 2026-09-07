#!/usr/bin/env python3
"""Turn the latest scrape into the repo's derived artifacts.

Reads data.json + meta.json (written by scrape.sh) and produces:
  logs.json     append-only log registry; array position is a stable series index
  history.jsonl one line per scrape, delays aligned to logs.json, rolling window
  metrics.txt   Prometheus text exposition of the current snapshot

Log names come from Cert Spotter's ALL.json, which spells them more readably than
Chrome does ("Google Argon 2026h2" vs "Google 'Argon2026h2' log"). Whether a log
is active comes from its state in Chrome's own all_logs_list.json. Note that both
files split logs across "logs" (RFC 6962) and "tiled_logs" (static-CT); reading
only the former silently misses about half of them.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

NAMES_URL = "https://loglist.certspotter.org/ALL.json"   # readable names for every log
STATE_URL = "https://www.gstatic.com/ct/log_list/v3/all_logs_list.json"  # Chrome's own states

# Chrome's CT log states. A log counts as active in these four; "rejected" and "retired"
# do not, nor does a log carrying no state or missing from the list altogether.
ACTIVE_STATES = frozenset({"pending", "qualified", "usable", "readonly"})
WINDOW_DAYS = 14          # how much history index.html fetches; git holds the rest
METRIC_PREFIX = "sct_auditing"

HERE = os.path.dirname(os.path.abspath(__file__))


def path(*p):
    return os.path.join(HERE, *p)


def parse_ts(s):
    """RFC 3339 -> aware datetime. The server's `now` has nanoseconds; datetime caps at micros."""
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6].ljust(6, '0')}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def load_json(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def each_log(listing):
    """Both list files split logs across `logs` and `tiled_logs`; reading one misses half."""
    for op in listing.get("operators", []):
        for log in list(op.get("logs") or []) + list(op.get("tiled_logs") or []):
            if "log_id" in log:
                yield log


def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def fetch_registry_info():
    """id -> {name, state, active}. Falls back to the committed registry if a list is unreachable."""
    try:
        names = {log["log_id"]: log.get("description") or log["log_id"][:12]
                 for log in each_log(get_json(NAMES_URL))}
        states = {}
        for log in each_log(get_json(STATE_URL)):
            keys = list((log.get("state") or {}).keys())
            states[log["log_id"]] = keys[0] if keys else "none"
    except Exception as e:                      # noqa: BLE001 - any failure falls back
        print(f"warning: could not fetch log lists ({e}); reusing known values", file=sys.stderr)
        return None
    if not states:
        print("warning: Chrome log list came back empty; reusing known values", file=sys.stderr)
        return None

    info = {}
    for log_id in set(names) | set(states):
        state = states.get(log_id, "absent")     # reported by Chrome's auditor, not in its list
        info[log_id] = {"name": names.get(log_id) or log_id[:12],
                        "state": state,
                        "active": state in ACTIVE_STATES}
    return info


def escape(v):
    """Prometheus label-value escaping."""
    return v.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def write_metrics(observations, meta, active, states):
    p = METRIC_PREFIX
    out = []

    def block(name, help_text, samples):
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} gauge")
        out.extend(samples)

    if observations:
        labels = {
            o["id"]: 'log_id="{}",log_name="{}",state="{}",active="{}"'.format(
                escape(o["id"]), escape(o["name"]), escape(states.get(o["id"], "absent")),
                "true" if active.get(o["id"]) else "false")
            for o in observations
        }
        block(f"{p}_ingestion_delay_seconds",
              "How far behind the SCT auditing service's ingestion point is for a CT log.",
              [f'{p}_ingestion_delay_seconds{{{labels[o["id"]]}}} {o["delay"]:.3f}'
               for o in observations])
        block(f"{p}_ingested_until_seconds",
              "Unix time of the point up to which a CT log has been ingested.",
              [f'{p}_ingested_until_seconds{{{labels[o["id"]]}}} {o["ingested_until"]:.3f}'
               for o in observations])

    block(f"{p}_logs_total", "Number of logs reported by the last successful scrape.",
          [f"{p}_logs_total {len(observations)}"])
    block(f"{p}_scrape_success", "Whether the most recent scrape returned a valid response.",
          [f'{p}_scrape_success {1 if meta.get("ok") else 0}'])
    block(f"{p}_scrape_timestamp_seconds", "Unix time of the most recent scrape attempt.",
          [f'{p}_scrape_timestamp_seconds {parse_ts(meta["scraped_at"]).timestamp():.0f}'])

    with open(path("metrics.txt"), "w") as f:
        f.write("\n".join(out) + "\n")


def main():
    meta = load_json(path("meta.json"))
    if meta is None:
        sys.exit("meta.json missing; run scrape.sh first")

    registry = load_json(path("logs.json"), {"logs": []})
    known = {entry["id"]: entry for entry in registry["logs"]}

    fresh = fetch_registry_info()
    if fresh:
        for entry in registry["logs"]:                    # refresh names in place
            if entry["id"] in fresh:
                entry.update(fresh[entry["id"]])

    if not meta.get("ok"):
        # Record the failure in the metrics, but add nothing to the history.
        print("last scrape failed; writing metrics only", file=sys.stderr)
        write_metrics([], meta, {}, {})
        return

    data = load_json(path("data.json"))
    reference = parse_ts(meta.get("server_now") or meta["scraped_at"])

    observations = []
    for entry in data["logStatus"]:
        log_id = entry["logId"]
        if log_id not in known:                           # append-only: indices never shift
            info = (fresh or {}).get(log_id, {})
            record = {"id": log_id,
                      "name": info.get("name") or log_id[:12],
                      "state": info.get("state", "absent"),
                      "active": info.get("active", False)}
            registry["logs"].append(record)
            known[log_id] = record
        ingested = parse_ts(entry["ingestedUntil"])
        observations.append({
            "id": log_id,
            "name": known[log_id]["name"],
            "delay": (reference - ingested).total_seconds(),
            "ingested_until": ingested.timestamp(),
        })

    active = {entry["id"]: bool(entry.get("active")) for entry in registry["logs"]}
    states = {entry["id"]: entry.get("state", "absent") for entry in registry["logs"]}

    with open(path("logs.json"), "w") as f:
        json.dump(registry, f, indent=1, sort_keys=True)
        f.write("\n")

    # History rows align to logs.json order; absent logs are null.
    index = {entry["id"]: i for i, entry in enumerate(registry["logs"])}
    delays = [None] * len(registry["logs"])
    for o in observations:
        delays[index[o["id"]]] = round(o["delay"])

    row = {"t": int(reference.timestamp()), "d": delays}
    cutoff = reference.timestamp() - WINDOW_DAYS * 86400
    kept = [ln for ln in (load_text(path("history.jsonl")) or "").splitlines()
            if ln.strip() and json.loads(ln)["t"] >= cutoff]
    kept.append(json.dumps(row, separators=(",", ":")))
    with open(path("history.jsonl"), "w") as f:
        f.write("\n".join(kept) + "\n")

    write_metrics(observations, meta, active, states)
    live = sum(1 for o in observations if active[o["id"]])
    print(f"{len(observations)} logs ({live} active), {len(kept)} history rows")


def load_text(p):
    try:
        with open(p) as f:
            return f.read()
    except FileNotFoundError:
        return None


if __name__ == "__main__":
    main()
