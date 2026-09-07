#!/usr/bin/env python3
"""Headless-browser tests for index.html.

Serves a throwaway copy of the page over HTTP (fetch() is blocked on file://)
backed by synthetic history, so the chart always has enough data to draw and the
repo's real files are never touched. The log-name fetch hits Cert Spotter for
real, which is what exercises the CORS path the page depends on.

    python3 -m venv venv && ./venv/bin/pip install playwright
    ./venv/bin/playwright install chromium
    ./venv/bin/python test_page.py [--screenshots DIR]
"""
import argparse
import functools
import http.server
import json
import math
import os
import random
import shutil
import socketserver
import sys
import tempfile
import threading

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = 672          # 7 days at the 15-minute cadence


def build_fixture(dest):
    for name in ("index.html", "logs.json"):
        shutil.copy(os.path.join(HERE, name), dest)
    logs = json.load(open(os.path.join(dest, "logs.json")))["logs"]

    random.seed(11)
    now, rows = 1_788_800_000, []
    gap = [i for i, l in enumerate(logs) if l.get("active")][-1]
    for k in range(SAMPLES):
        row = []
        for i, log in enumerate(logs):
            if not log.get("active"):
                row.append(3_000_000 + k * 900)        # inactive: grows without bound
            elif i == gap and k < 400:
                row.append(None)                       # appears partway through
            else:
                row.append(max(1, int(math.exp(random.gauss(7.0, 0.9)))))
        rows.append({"t": now - (SAMPLES - 1 - k) * 900, "d": row})

    with open(os.path.join(dest, "history.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n")
    return sum(1 for l in logs if l.get("active")), len(logs)


def serve(directory):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)

    class Quiet(handler.func):
        def log_message(self, *a): pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=directory))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--screenshots", metavar="DIR", help="write PNGs of each state here")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="sctpage-")
    active, total = build_fixture(tmp)
    port = serve(tmp)
    url = f"http://127.0.0.1:{port}/index.html"

    results = []

    def check(ok, msg):
        results.append(ok)
        print(("PASS  " if ok else "FAIL  ") + msg)

    def shot(page, name):
        if args.screenshots:
            os.makedirs(args.screenshots, exist_ok=True)
            page.screenshot(path=os.path.join(args.screenshots, name), full_page=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for theme in ("light", "dark"):
            page = browser.new_page(viewport={"width": 1280, "height": 900}, color_scheme=theme)
            errors, failed = [], []
            page.on("console", lambda m: m.type == "error" and errors.append(m.text))
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("requestfailed", lambda r: failed.append(r.url))

            page.goto(url, wait_until="networkidle")
            page.wait_for_selector("#chart canvas", timeout=15000)

            print(f"\n--- {theme} ---")
            check(not errors, f"no console or page errors {errors[:2]}")
            check(not failed, f"no failed requests {failed[:2]}")
            shot(page, f"{theme}-default.png")

            if theme == "dark":
                bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
                check(bg != "rgb(255, 255, 255)", f"dark palette applied (body {bg})")
                page.close()
                continue

            names = page.eval_on_selector_all("#legend .nm", "els => els.map(e => e.textContent)")
            check(len(names) == active, f"legend lists {len(names)} active logs (expect {active})")
            check(any("Argon" in n or "Nimbus" in n for n in names),
                  f"names came from Cert Spotter over CORS (e.g. {names[0]!r})")

            box = page.locator("#chart canvas").bounding_box()
            check(box["height"] > 300 and box["width"] > 600,
                  f"canvas {int(box['width'])}x{int(box['height'])}")

            # Axis labels are painted to canvas; a CSS var here would silently vanish.
            labels = page.eval_on_selector_all(
                ".u-axis", "els => els.length")
            check(labels >= 2, f"both axes rendered ({labels})")

            page.fill("#filter", "nimbus")
            page.wait_for_timeout(300)
            n = page.eval_on_selector_all("#legend .item", "e => e.length")
            check(0 < n < active, f"filter narrows legend to {n}")
            shot(page, "light-filter.png")
            page.fill("#filter", "")
            page.wait_for_timeout(300)

            page.check("#inactive")
            page.wait_for_timeout(400)
            n = page.eval_on_selector_all("#legend .item", "e => e.length")
            struck = page.eval_on_selector_all("#legend .item.inactive", "e => e.length")
            check(n == total, f"including inactive shows all {n} logs")
            check(struck == total - active, f"{struck} rows marked inactive")
            shot(page, "light-inactive.png")
            page.uncheck("#inactive")
            page.wait_for_timeout(300)

            for span in ("21600", "604800", "0"):
                page.select_option("#range", span)
                page.wait_for_timeout(350)
            check(not errors, "range switching raised no errors")

            box = page.locator("#chart canvas").bounding_box()
            page.mouse.move(box["x"] + box["width"] * 0.35, box["y"] + box["height"] * 0.5)
            page.wait_for_timeout(350)
            hovered = page.eval_on_selector("#legend .val", "e => e.textContent")
            check(hovered not in ("", "—"), f"hover writes values into the legend ({hovered!r})")
            shot(page, "light-hover.png")

            page.click("#legend .item")
            page.wait_for_timeout(300)
            check(page.eval_on_selector_all("#legend .item.off", "e => e.length") == 1,
                  "clicking a legend row toggles that series off")

            page.click("#none")
            page.wait_for_timeout(300)
            check("No logs selected" in page.inner_text("#chart"), "'None' shows the empty state")
            page.click("#all")
            page.wait_for_timeout(400)
            check(page.eval_on_selector_all("#chart canvas", "e => e.length") == 1, "'All' redraws")

            for width in (1280, 390):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(400)
                check(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
                      f"no horizontal overflow at {width}px")
            shot(page, "light-mobile.png")
            page.close()
        browser.close()

    shutil.rmtree(tmp, ignore_errors=True)
    failures = results.count(False)
    print(f"\n{results.count(True)} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
