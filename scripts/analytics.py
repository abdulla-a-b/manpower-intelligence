#!/usr/bin/env python3
"""
Shoe Industry — Manpower / Payroll / Overtime analytics
Technology Partner: Guulba (https://www.guulba.com)

Pulls the live records from the Apps Script web app (same read endpoint the
dashboard uses), recomputes the costing model exactly as the front-end does,
and writes docs/data/insights.json. A scheduled GitHub Action runs this so the
dashboard ships with pre-computed management insights and compliance flags
even before the browser does any work.

Costing model (must stay in sync with docs/index.html):
  Bangladesh Labour Act 2006, §108
    OT hourly rate = 2 * (basic / 208)        # 208 = 26 working days * 8 h
    OT hours       = present * otHead          # otHead = OT hrs per present head/day
    OT cost        = OT hours * OT hourly rate
  Monthly-prorated payroll
    payroll        = hc * gross / 26
  §102 weekly ceiling
    A worker may not exceed ~12 OT hours/week (60 h cap - 48 h normal).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.request
from collections import defaultdict

WORKING_DAYS_MONTH = 26
OT_DIVISOR = 208            # 26 days * 8 hours
OT_MULTIPLIER = 2          # double rate
WEEKLY_OT_CEILING = 12.0   # hours/worker/week (60 - 48)

OUTPUT = os.path.join("docs", "data", "insights.json")


# ----------------------------------------------------------------------
# Costing primitives (mirror of recDay() in the front-end)
# ----------------------------------------------------------------------
def ot_rate(basic: float) -> float:
    return OT_MULTIPLIER * (basic / OT_DIVISOR)


def rec_day(r: dict) -> dict:
    hc = float(r.get("hc", 0) or 0)
    present = float(r.get("present", 0) or 0)
    basic = float(r.get("basic", 0) or 0)
    gross = float(r.get("gross", 0) or 0)
    ot_head = float(r.get("otHead", 0) or 0)
    ot_hours = present * ot_head
    return {
        "payroll": hc * gross / WORKING_DAYS_MONTH,
        "otHours": ot_hours,
        "otCost": ot_hours * ot_rate(basic),
        "present": present,
        "hc": hc,
    }


def iso_week_key(date_str: str) -> str:
    y, m, d = (int(x) for x in date_str[:10].split("-"))
    iso = dt.date(y, m, d).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def month_key(date_str: str) -> str:
    return date_str[:7]


# ----------------------------------------------------------------------
# Data source
# ----------------------------------------------------------------------
def fetch_records() -> list[dict]:
    url = os.environ.get("APPS_SCRIPT_URL", "").strip()
    if not url:
        print("APPS_SCRIPT_URL not set — emitting empty insights "
              "(dashboard will use demo data).")
        return []
    read_url = url + ("&" if "?" in url else "?") + "action=read"
    try:
        req = urllib.request.Request(read_url, headers={"User-Agent": "guulba-analytics"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        records = payload.get("records", []) if isinstance(payload, dict) else []
        print(f"Fetched {len(records)} records from Apps Script.")
        return records
    except Exception as exc:                      # noqa: BLE001
        print(f"::warning::Could not fetch live records ({exc}). Emitting empty insights.")
        return []


# ----------------------------------------------------------------------
# Aggregations
# ----------------------------------------------------------------------
def latest_month(records: list[dict]) -> str | None:
    months = sorted({month_key(r["date"]) for r in records if r.get("date")})
    # drop the trailing partial month if it is clearly incomplete
    if len(months) >= 2:
        last_days = len({r["date"] for r in records if month_key(r["date"]) == months[-1]})
        prev_days = len({r["date"] for r in records if month_key(r["date"]) == months[-2]})
        if prev_days and last_days < 0.7 * prev_days:
            return months[-2]
    return months[-1] if months else None


def summarise(records: list[dict], month: str) -> dict:
    by_plant = defaultdict(lambda: {"payroll": 0.0, "otCost": 0.0, "otHours": 0.0, "hc": 0.0, "days": set()})
    by_dept = defaultdict(lambda: {"payroll": 0.0, "otCost": 0.0, "otHours": 0.0})
    totals = {"payroll": 0.0, "otCost": 0.0, "otHours": 0.0, "hc": 0.0, "present": 0.0, "days": set()}

    for r in records:
        if not r.get("date") or month_key(r["date"]) != month:
            continue
        d = rec_day(r)
        p = by_plant[r.get("plant", "—")]
        p["payroll"] += d["payroll"]; p["otCost"] += d["otCost"]
        p["otHours"] += d["otHours"]; p["hc"] += d["hc"]; p["days"].add(r["date"])
        dep = by_dept[r.get("dept", "—")]
        dep["payroll"] += d["payroll"]; dep["otCost"] += d["otCost"]; dep["otHours"] += d["otHours"]
        totals["payroll"] += d["payroll"]; totals["otCost"] += d["otCost"]
        totals["otHours"] += d["otHours"]; totals["hc"] += d["hc"]
        totals["present"] += d["present"]; totals["days"].add(r["date"])

    nd = max(len(totals["days"]), 1)
    plants = {k: {
        "payroll": round(v["payroll"]),
        "otCost": round(v["otCost"]),
        "otHours": round(v["otHours"]),
        "avgHeadcount": round(v["hc"] / max(len(v["days"]), 1)),
    } for k, v in sorted(by_plant.items())}

    depts = {k: {
        "payroll": round(v["payroll"]),
        "otCost": round(v["otCost"]),
        "otHours": round(v["otHours"]),
    } for k, v in sorted(by_dept.items(), key=lambda kv: -kv[1]["otCost"])}

    return {
        "month": month,
        "totals": {
            "avgHeadcount": round(totals["hc"] / nd),
            "attendancePct": round(totals["present"] / totals["hc"] * 100, 1) if totals["hc"] else 0,
            "payroll": round(totals["payroll"]),
            "otCost": round(totals["otCost"]),
            "otHours": round(totals["otHours"]),
        },
        "byPlant": plants,
        "byDept": depts,
    }


def compliance(records: list[dict], month: str) -> list[dict]:
    """Per line/week: average OT hours per present worker vs §102 ceiling."""
    agg = defaultdict(lambda: {"ot": 0.0, "presentDays": 0.0, "plant": "", "unit": "", "dept": ""})
    for r in records:
        if not r.get("date") or month_key(r["date"]) != month:
            continue
        seg = r.get("seg", "Production")
        if seg != "Production":
            continue
        wk = iso_week_key(r["date"])
        key = (r.get("plant", ""), r.get("unit", ""), r.get("dept", ""), r.get("line", ""), wk)
        a = agg[key]
        a["ot"] += float(r.get("present", 0) or 0) * float(r.get("otHead", 0) or 0)
        a["presentDays"] += float(r.get("present", 0) or 0)
        a["plant"], a["unit"], a["dept"] = r.get("plant", ""), r.get("unit", ""), r.get("dept", "")

    rows = []
    for (plant, unit, dept, line, wk), a in agg.items():
        # average OT hours per present worker across the week's working days
        avg_days = a["presentDays"] / 6 if a["presentDays"] else 0   # ~6 working days/week
        per_worker = (a["ot"] / avg_days) if avg_days else 0
        status = "ok" if per_worker <= WEEKLY_OT_CEILING else \
                 ("warn" if per_worker <= WEEKLY_OT_CEILING * 1.25 else "bad")
        rows.append({
            "plant": plant, "unit": unit, "dept": dept, "line": line, "week": wk,
            "otHoursPerWorker": round(per_worker, 1), "status": status,
        })
    rows.sort(key=lambda x: -x["otHoursPerWorker"])
    return rows


def build_insights(records: list[dict]) -> dict:
    generated = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if not records:
        return {"generatedAt": generated, "live": False, "records": 0,
                "summary": None, "compliance": [], "alerts": []}

    month = latest_month(records)
    summary = summarise(records, month)
    comp = compliance(records, month)
    breaches = [c for c in comp if c["status"] == "bad"]
    alerts = []
    if breaches:
        alerts.append({
            "level": "critical",
            "message": f"{len(breaches)} line-week(s) exceed the §102 weekly OT ceiling "
                       f"of {WEEKLY_OT_CEILING:.0f} h/worker in {month}.",
        })
    if summary["totals"]["attendancePct"] and summary["totals"]["attendancePct"] < 88:
        alerts.append({
            "level": "warn",
            "message": f"Attendance at {summary['totals']['attendancePct']}% in {month} "
                       f"— below the 88% planning threshold.",
        })
    return {
        "generatedAt": generated,
        "live": True,
        "records": len(records),
        "summary": summary,
        "compliance": comp[:50],
        "alerts": alerts,
    }


def main() -> int:
    records = fetch_records()
    insights = build_insights(records)
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as fh:
        json.dump(insights, fh, indent=2)
    print(f"Wrote {OUTPUT}: live={insights['live']} records={insights['records']} "
          f"alerts={len(insights['alerts'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
