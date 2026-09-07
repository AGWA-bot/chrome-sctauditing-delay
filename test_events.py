#!/usr/bin/env python3
"""Tests for incident detection and the Atom feed. Run: ./test_events.py"""
import copy
import json
import os
import tempfile
import xml.etree.ElementTree as ET

import events

ATOM = "{http://www.w3.org/2005/Atom}"
HOUR, DAY = 3600, 86400
T0 = 1_780_000_000            # arbitrary fixed epoch, keeps output deterministic

REGISTRY = {"logs": [
    {"id": "AAAA", "name": "Active One", "active": True},
    {"id": "BBBB", "name": "Active Two", "active": True},
    {"id": "CCCC", "name": "Retired Log", "active": False},
]}

results = []


def check(cond, msg):
    results.append(cond)
    print(("PASS  " if cond else "FAIL  ") + msg)


def rows(spec):
    """spec: list of (offset_seconds, [delay_a, delay_b, delay_c])."""
    return [{"t": T0 + off, "d": list(d)} for off, d in spec]


def run(spec, state=None, now=None):
    state = state if state is not None else events.load_state("/nonexistent")
    data = rows(spec)
    now = now if now is not None else data[-1]["t"]
    return events.update(state, data, REGISTRY, now, "example.test")


OK = 600            # 10 minutes: comfortably under the threshold
OVER = 30 * HOUR    # over the 24h threshold

# --- opening -------------------------------------------------------------
st = run([(0, [OK, OK, OVER]), (HOUR, [OVER, OK, OVER])])
check(len(st["events"]) == 1, f"one event opened ({len(st['events'])})")
check(st["events"][0]["log_id"] == "AAAA", "opened for the log that crossed the threshold")
check(all(e["log_id"] != "CCCC" for e in st["events"]),
      "an inactive log never opens an event even though it is always over")
check(st["events"][0]["published"] == events.iso(T0 + HOUR),
      "published is the sample where it crossed, not the run start of anything else")

# --- one event per log per week -----------------------------------------
spec = [(0, [OVER, OK, OK]),          # run 1 opens
        (HOUR, [OK, OK, OK]),         # recovers
        (2 * DAY, [OVER, OK, OK]),    # run 2, still inside the 7-day cooldown
        (2 * DAY + HOUR, [OK, OK, OK])]
st = run(spec)
check(len(st["events"]) == 1, f"second run inside cooldown folds in, still one event ({len(st['events'])})")
check(len(st["events"][0]["runs"]) == 2, "the event records both runs")
check("2 episodes" in events.describe(st["events"][0])[0],
      f"title reflects the fold: {events.describe(st['events'][0])[0]!r}")

st = run(spec + [(9 * DAY, [OVER, OK, OK])])
check(len(st["events"]) == 2, f"a run after the cooldown opens a new event ({len(st['events'])})")
check(st["events"][0]["id"] != st["events"][1]["id"], "the new event gets its own id")

# --- timestamps are immutable -------------------------------------------
first = run([(0, [OVER, OK, OK])])
snapshot = copy.deepcopy(first["events"][0])
later = run([(0, [OVER, OK, OK]), (HOUR, [OK] * 3), (DAY, [40 * HOUR, OK, OK])], state=first)
ev = later["events"][0]
check(ev["id"] == snapshot["id"], "entry id unchanged after folding")
check(ev["published"] == snapshot["published"], "published unchanged after folding")
check(ev["peak_delay"] > snapshot["peak_delay"],
      f"but the content did change (peak {snapshot['peak_delay']} -> {ev['peak_delay']})")

with tempfile.TemporaryDirectory() as d:
    f = os.path.join(d, "feed.xml")
    events.write_feed(f, later, "https://example.test/x")
    entry = ET.parse(f).getroot().find(f"{ATOM}entry")
    check(entry.find(f"{ATOM}published").text == entry.find(f"{ATOM}updated").text,
          "feed entry: updated == published, so Slack will not repost it")

# --- a single run is reported once --------------------------------------
st = run([(i * HOUR, [OVER, OK, OK]) for i in range(9 * 24)])   # 9 continuous days over
check(len(st["events"]) == 1,
      f"one unbroken 9-day run yields exactly one event, not one per week ({len(st['events'])})")

# --- idempotency ---------------------------------------------------------
spec = [(0, [OVER, OK, OK]), (HOUR, [OK] * 3)]
once = run(spec)
twice = run(spec, state=copy.deepcopy(once))
check(json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True),
      "re-running over the same history is a no-op")

# --- two logs are independent -------------------------------------------
st = run([(0, [OVER, OVER, OK])])
check(len(st["events"]) == 2, f"each log gets its own event ({len(st['events'])})")

# --- retention -----------------------------------------------------------
st = run([(0, [OVER, OK, OK])])
st = events.update(st, rows([(0, [OVER, OK, OK])]), REGISTRY, T0 + 200 * DAY, "example.test")
check(st["events"] == [], "events older than the retention window are pruned")

# --- feed validity -------------------------------------------------------
st = run([(0, [OVER, OVER, OK]), (2 * DAY, [OVER, OK, OK])])
with tempfile.TemporaryDirectory() as d:
    f = os.path.join(d, "feed.xml")
    events.write_feed(f, st, "https://example.test/x")
    root = ET.parse(f).getroot()
    check(root.tag == f"{ATOM}feed", "feed parses as Atom")
    for tag in ("id", "title", "updated"):
        check(root.find(ATOM + tag) is not None, f"feed has required <{tag}>")
    entries = root.findall(f"{ATOM}entry")
    check(len(entries) == 2, f"both entries present ({len(entries)})")
    for tag in ("id", "title", "updated", "published"):
        check(all(e.find(ATOM + tag) is not None for e in entries), f"entries have <{tag}>")
    check(all(e.find(f"{ATOM}id").text.startswith("tag:example.test,") for e in entries),
          "entry ids are tag URIs under the configured authority")
    check(len({e.find(f"{ATOM}id").text for e in entries}) == 2, "entry ids are unique")
    check(root.find(f"{ATOM}updated").text == max(
        e.find(f"{ATOM}updated").text for e in entries),
        "feed updated tracks the newest entry")

# --- escaping ------------------------------------------------------------
weird = {"logs": [{"id": "X&Y", "name": 'Log <b>"&" bad</b>', "active": True}]}
st = events.update(events.load_state("/nonexistent"),
                   [{"t": T0, "d": [OVER]}], weird, T0, "example.test")
with tempfile.TemporaryDirectory() as d:
    f = os.path.join(d, "feed.xml")
    events.write_feed(f, st, "https://example.test/x")
    title = ET.parse(f).getroot().find(f"{ATOM}entry/{ATOM}title").text
    check("<b>" in title and "&" in title, f"markup in a log name is escaped, not injected: {title!r}")

print(f"\n{results.count(True)} passed, {results.count(False)} failed")
raise SystemExit(1 if not all(results) else 0)
