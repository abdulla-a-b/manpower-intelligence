#!/usr/bin/env python3
"""Inject the Apps Script web-app URL into the built dashboard.

Reads the URL from the APPS_SCRIPT_URL environment variable (set by the
GitHub Actions workflow from repo secrets) and replaces the
``__APPS_SCRIPT_URL__`` placeholder in the target HTML file.

If the secret is empty, the placeholder is left untouched so the site
falls back to bundled demo data — the build still succeeds.

Usage:  python3 scripts/inject_url.py docs/index.html
"""
import os
import sys
import pathlib

PLACEHOLDER = "https://script.google.com/macros/s/AKfycbxrBWGP_Y4tbPuynrZGCoD5ARlIPtHgrk9LT6L6HmBh6eJ-AUB_iNno8uqDIn6_LFYbyQ/exec"


def main() -> int:
    target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "docs/index.html")
    url = os.environ.get("APPS_SCRIPT_URL", "").strip()

    if not target.exists():
        print(f"::error::Target file not found: {target}")
        return 1

    if not url:
        print("::warning::APPS_SCRIPT_URL secret is empty — "
              "leaving placeholder; site will run on bundled demo data.")
        return 0

    html = target.read_text(encoding="utf-8")
    count = html.count(PLACEHOLDER)
    if count == 0:
        print("::warning::Placeholder not found — already injected or markup changed.")
        return 0

    html = html.replace(PLACEHOLDER, url)
    target.write_text(html, encoding="utf-8")
    print(f"Injected Apps Script URL into {target} ({count} occurrence(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
