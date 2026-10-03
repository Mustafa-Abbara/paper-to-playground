#!/usr/bin/env python3
"""DEV-ONLY browser smoke test. Never imported by agent.py.

Needs: pip install playwright && python -m playwright install chromium

    python tools/smoke_browser.py runs/fixture/index.html [--shots runs/shots]

Serves the page on localhost, BLOCKS every other request (simulates offline),
operates every control once, and reports console errors, blocked requests,
whether the readout changed, and horizontal overflow on a phone-sized screen.
Exit 0 = clean, 1 = problems found.
"""
import argparse
import functools
import http.server
import json
import os
import sys
import threading


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("html")
    ap.add_argument("--shots", default=None, help="folder for screenshots")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright  # dev dependency only

    root = os.path.dirname(os.path.abspath(args.html))
    name = os.path.basename(args.html)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port),
                                          functools.partial(Quiet, directory=root))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{args.port}/"
    errors, blocked, report = [], [], {}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        def route(r):
            if r.request.url.startswith(base):
                r.continue_()
            else:
                blocked.append(r.request.url)
                r.abort()

        page.route("**/*", route)
        page.goto(base + name)
        page.wait_for_timeout(300)
        report["error_box"] = page.inner_text("#error") if page.is_visible("#error") else ""
        controls = page.locator("[data-control]")
        report["controls"] = controls.count()
        changed = 0
        for i in range(controls.count()):
            c = controls.nth(i)
            before = page.inner_text("#playground")
            rng = c.locator("input[type=range]")
            if rng.count():
                rng.first.focus()
                page.keyboard.press("End")
            elif c.locator("input[type=checkbox]").count():
                c.locator("input[type=checkbox]").first.click()
            elif c.locator("select").count():
                opts = c.locator("select option")
                if opts.count() > 1:
                    c.locator("select").first.select_option(index=opts.count() - 1)
            elif c.locator("input").count():
                inp = c.locator("input").first
                inp.fill("0.7")
            page.wait_for_timeout(50)
            if page.inner_text("#playground") != before:
                changed += 1
        report["controls_that_changed_output"] = changed
        for i, b in enumerate(page.locator(".explore > .btn").all()):
            b.click()
            page.wait_for_timeout(50)
            report[f"exploration_{i + 1}_error_box"] = page.is_visible("#error")
        page.click("#reset")
        if args.shots:
            os.makedirs(args.shots, exist_ok=True)
            page.screenshot(path=os.path.join(args.shots, "desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(100)
        report["phone_overflow_px"] = page.evaluate(
            "document.documentElement.scrollWidth - window.innerWidth")
        if args.shots:
            page.screenshot(path=os.path.join(args.shots, "phone.png"), full_page=True)
        browser.close()
    srv.shutdown()

    report["console_errors"] = errors
    report["blocked_requests"] = blocked
    print(json.dumps(report, indent=1, ensure_ascii=False))
    bad = (errors or blocked or report["error_box"] or report["phone_overflow_px"] > 0
           or report["controls_that_changed_output"] < 2)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
