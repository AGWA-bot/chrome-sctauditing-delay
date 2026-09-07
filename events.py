#!/usr/bin/env python3
"""Incident detection and Atom feed.

A log whose ingestion delay exceeds `THRESHOLD` is in an incident. Consecutive
samples over the threshold form a run, and each run is reported at most once. A
log gets at most one event per `COOLDOWN`: while an event is open, later runs
fold into it, updating the peak delay and the window it covers.

Entry timestamps never change after publication. `published` and `updated` are
both frozen when the event is created and are never rewritten, even as the event
absorbs later runs and its title and body change. Feed readers -- Slack in
particular -- repost an entry whose `updated` moves, so this is the property that
keeps a long-running incident from being announced over and over.

Only active logs are considered. A retired log's delay grows without bound by
design, so including them would open an incident for every one of them forever.
"""
import json
import os
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

THRESHOLD = 24 * 3600          # a delay over this is an incident
COOLDOWN = 7 * 24 * 3600       # at most one event per log per week
RETENTION = 90 * 24 * 3600     # how long events stay in the feed


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def human(seconds):
    """Coarse duration for prose: '3d 4h', '31h', '25h'."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours = rem // 3600
    if days and hours:
        return f"{days}d {hours}h"
    if days:
        return f"{days}d"
    return f"{hours}h"


def build_runs(rows, count):
    """runs[i] = list of (start_ts, [(t, delay), ...]) for log index i."""
    runs = [[] for _ in range(count)]
    open_run = [None] * count
    for row in rows:
        delays = row["d"]
        for i in range(count):
            v = delays[i] if i < len(delays) else None
            over = v is not None and v > THRESHOLD
            if over:
                if open_run[i] is None:
                    open_run[i] = (row["t"], [])
                    runs[i].append(open_run[i])
                open_run[i][1].append((row["t"], v))
            else:
                open_run[i] = None
    return runs


def condition_at(runs_for_log, t):
    """The run covering time t, as (start, peak_so_far, delay_at_t), or None."""
    for start, samples in runs_for_log:
        for k, (ts, v) in enumerate(samples):
            if ts == t:
                return start, max(s[1] for s in samples[:k + 1]), v
        if samples and samples[-1][0] > t:
            break
    return None


def tag_id(authority, log_id, published):
    """RFC 4151 tag URI. Permanent: changing it makes every reader repost."""
    safe = "".join(c if c.isalnum() else "-" for c in log_id)[:16]
    day = datetime.fromtimestamp(published, timezone.utc).strftime("%Y-%m-%d")
    return f"tag:{authority},{day}:chrome-sctauditing-delay/{safe}/{int(published)}"


def open_event(events, log_id, now):
    """The log's most recent event, if it is still inside its cooldown."""
    for event in reversed(events):
        if event["log_id"] == log_id:
            return event if now - parse_iso(event["published"]) < COOLDOWN else None
    return None


def update(state, rows, registry, now, authority):
    """Advance `state` over every sample newer than the last evaluation."""
    logs = registry["logs"]
    active = [bool(l.get("active")) for l in logs]
    names = {l["id"]: l["name"] for l in logs}
    runs = build_runs(rows, len(logs))

    evaluated = parse_iso(state["evaluated"]) if state.get("evaluated") else 0
    times = sorted({r["t"] for r in rows if r["t"] > evaluated and r["t"] <= now})

    for t in times:
        for i, log in enumerate(logs):
            if not active[i]:
                continue
            hit = condition_at(runs[i], t)
            if hit is None:
                continue
            start, peak, current = hit
            key = f"{log['id']} {start}"
            event = open_event(state["events"], log["id"], t)
            fresh = key not in state["reported"]

            if fresh:
                if event is None:
                    event = {
                        "id": tag_id(authority, log["id"], t),
                        "log_id": log["id"],
                        "log_name": names.get(log["id"], log["id"][:12]),
                        # published and updated are frozen here, for good.
                        "published": iso(t),
                        "since": iso(start),
                        "runs": [],
                        "peak_delay": 0,
                        "last_delay": 0,
                        "last_seen": iso(t),
                    }
                    state["events"].append(event)
                state["reported"][key] = iso(start)
                event["runs"].append(start)
            elif event is None or start not in event["runs"]:
                continue    # already reported into an event that has since closed

            if start < parse_iso(event["since"]):
                event["since"] = iso(start)
            event["peak_delay"] = max(event["peak_delay"], int(peak))
            event["last_delay"] = int(current)
            event["last_seen"] = iso(t)

    state["evaluated"] = iso(now)
    cutoff = now - RETENTION
    state["events"] = [e for e in state["events"] if parse_iso(e["published"]) >= cutoff]
    state["reported"] = {k: v for k, v in state["reported"].items() if parse_iso(v) >= cutoff}
    return state


def describe(event):
    """Title and body. These may change as an event absorbs runs; timestamps may not."""
    episodes = len(event["runs"])
    title = f"{event['log_name']}: ingestion delay reached {human(event['peak_delay'])}"
    if episodes > 1:
        title += f" ({episodes} episodes)"
    lines = [
        f"Ingestion delay for {event['log_name']} passed 24h.",
        f"First seen {event['since']}, last seen {event['last_seen']}.",
        f"Peak delay {human(event['peak_delay'])}; most recent sample {human(event['last_delay'])}.",
    ]
    if episodes > 1:
        lines.append(f"{episodes} separate episodes folded into this event.")
    return title, lines


def write_feed(path, state, site, title="Chrome SCT auditing incidents"):
    events = sorted(state["events"], key=lambda e: e["published"], reverse=True)
    # The feed's own updated tracks the newest entry, so it stays put when nothing new lands.
    feed_updated = events[0]["published"] if events else state["evaluated"]

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<feed xmlns="http://www.w3.org/2005/Atom">',
           f"  <title>{escape(title)}</title>",
           f"  <id>{escape(site)}/feed.xml</id>",
           f"  <updated>{feed_updated}</updated>",
           "  <author><name>chrome-sctauditing-delay</name></author>",
           f'  <link href={quoteattr(site + "/feed.xml")} rel="self" type="application/atom+xml"/>',
           f'  <link href={quoteattr(site + "/")} rel="alternate" type="text/html"/>']

    for event in events:
        entry_title, lines = describe(event)
        summary = "\n".join(lines)
        body = "\n".join(f"<p>{escape(line)}</p>" for line in lines)
        out += [
            "  <entry>",
            f"    <id>{escape(event['id'])}</id>",
            f"    <title>{escape(entry_title)}</title>",
            f'    <link href={quoteattr(site + "/")} rel="alternate" type="text/html"/>',
            # published == updated, and neither ever moves again.
            f"    <published>{event['published']}</published>",
            f"    <updated>{event['published']}</updated>",
            f'    <summary type="text">{escape(summary)}</summary>',
            f'    <content type="html">{escape(body)}</content>',
            "  </entry>",
        ]
    out.append("</feed>")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def load_state(path):
    try:
        with open(path) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("evaluated", None)
    state.setdefault("events", [])
    state.setdefault("reported", {})
    return state


def save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")
