#!/usr/bin/env python3
"""
Build an expenses.csv (report_url,date,merchant,amount,currency,category,description,receipt_path)
from a JSON list of transactions parsed from receipt screenshots.

Input JSON: a list of objects, one per screenshot, in the order the screenshots
were read/parsed:
  [
    {"date": "2026-06-01", "merchant": "Pret A Manger", "amount": 9.54, "receipt_path": "IMG_1639.PNG"},
    {"date": "2026-06-01", "merchant": "TFL",            "amount": 8.90, "receipt_path": "IMG_1640.PNG"},
    ...
  ]

Optional per-transaction overrides: "category", "description", "currency".

Rules applied, in order:
  1. Auto-category: merchant containing "TFL" or "Trainline" (case-insensitive) -> Travel,
     everything else -> Subsistence. Explicit "category" in the input overrides this.
  2. Subsistence per-expense cap: SUBSISTENCE_PER_EXPENSE_CAP (default £20). Amounts over
     the cap are reduced to it.
  3. Subsistence per-day cap: SUBSISTENCE_PER_DAY_CAP (default £40). Transactions are
     processed in input order per day; each one is capped to the remaining daily
     allowance. If nothing remains, the transaction is dropped (its receipt is not
     included in the output) and reported in the summary.
  4. Per Diem fill: for every date in [--start-date, --end-date] with a total
     Subsistence spend under PER_DIEM_THRESHOLD (default £15) -- including zero,
     i.e. no Subsistence rows at all that day -- the Subsistence rows for that day
     (if any) are replaced with a single Per Diem row of PER_DIEM_AMOUNT (default
     £15). Travel and other non-Subsistence rows for that day are left untouched.
     Days already covered only by Travel (or nothing) still get a Per Diem row.

Usage:
  python3 build_expenses.py --input june_transactions.json \
      --report-url 'https://www.expensify.com/report?param=...' \
      --start-date 2026-06-01 --end-date 2026-06-30 \
      --out expenses_JUNE.csv
"""
import argparse
import csv
import datetime
import json
import sys
from collections import defaultdict
from pathlib import Path

SUBSISTENCE_PER_EXPENSE_CAP = 20.00
SUBSISTENCE_PER_DAY_CAP = 40.00
PER_DIEM_THRESHOLD = 15.00
PER_DIEM_AMOUNT = 15.00

FIELDS = ["report_url", "date", "merchant", "amount", "currency",
          "category", "description", "receipt_path"]

TRAVEL_MERCHANT_KEYWORDS = ("tfl", "trainline")


def auto_category(merchant: str) -> str:
    m = merchant.lower()
    return "Travel" if any(k in m for k in TRAVEL_MERCHANT_KEYWORDS) else "Subsistence"


def per_diem_row(report_url: str, date: str) -> dict:
    return {
        "report_url": report_url, "date": date, "merchant": "Per Diem",
        "amount": "15.00", "currency": "GBP", "category": "Per Diem",
        "description": "Per Diem", "receipt_path": "",
    }


def build(transactions, report_url, start_date=None, end_date=None):
    """Returns (rows, summary) where summary lists capping/dropping/per-diem actions."""
    summary = []
    rows = []
    day_sub_running = defaultdict(float)  # running Subsistence total per day, in input order

    for t in transactions:
        date = t["date"]
        merchant = t["merchant"]
        amount = float(t["amount"])
        category = t.get("category") or auto_category(merchant)
        currency = (t.get("currency") or "GBP").upper()
        description = t.get("description") or merchant
        receipt_path = t.get("receipt_path", "")

        if category == "Subsistence":
            capped = min(amount, SUBSISTENCE_PER_EXPENSE_CAP)
            if capped != amount:
                summary.append(f"{date} {merchant}: capped £{amount:.2f} -> £{capped:.2f} (per-expense cap)")

            remaining = round(SUBSISTENCE_PER_DAY_CAP - day_sub_running[date], 2)
            if remaining <= 0:
                summary.append(f"{date} {merchant}: DROPPED (day cap £{SUBSISTENCE_PER_DAY_CAP:.2f} already reached)")
                continue
            final = min(capped, remaining)
            if final != capped:
                summary.append(f"{date} {merchant}: capped £{capped:.2f} -> £{final:.2f} (day cap)")
            day_sub_running[date] += final
            amount = final

        rows.append({
            "report_url": report_url, "date": date, "merchant": merchant,
            "amount": f"{amount:.2f}", "currency": currency, "category": category,
            "description": description, "receipt_path": receipt_path,
        })

    # Determine the date range to fill with Per Diem
    all_dates = {r["date"] for r in rows}
    if start_date and end_date:
        d = datetime.date.fromisoformat(start_date)
        end = datetime.date.fromisoformat(end_date)
        date_range = []
        while d <= end:
            date_range.append(d.isoformat())
            d += datetime.timedelta(days=1)
    else:
        date_range = sorted(all_dates)

    # Always check every date that actually appears in the input, even if it
    # falls outside [start_date, end_date] (e.g. a report that starts mid-week).
    date_range = sorted(set(date_range) | all_dates)

    sub_totals = defaultdict(float)
    for r in rows:
        if r["category"] == "Subsistence":
            sub_totals[r["date"]] += float(r["amount"])

    convert = {d for d in date_range if sub_totals.get(d, 0.0) < PER_DIEM_THRESHOLD}

    final_rows = [r for r in rows if not (r["date"] in convert and r["category"] == "Subsistence")]
    for d in sorted(convert):
        final_rows.append(per_diem_row(report_url, d))
        summary.append(f"{d}: Per Diem £{PER_DIEM_AMOUNT:.2f} added (Subsistence was £{sub_totals.get(d, 0.0):.2f})")

    final_rows.sort(key=lambda r: (r["date"], r["category"]))
    return final_rows, summary


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="JSON file with parsed transactions")
    p.add_argument("--report-url", required=True)
    p.add_argument("--start-date", help="YYYY-MM-DD, for Per Diem fill range (defaults to min/max of input dates)")
    p.add_argument("--end-date", help="YYYY-MM-DD, for Per Diem fill range")
    p.add_argument("--out", required=True, help="output CSV path")
    args = p.parse_args()

    transactions = json.loads(Path(args.input).read_text())
    rows, summary = build(transactions, args.report_url, args.start_date, args.end_date)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    for line in summary:
        print(line)
    print(f"\nWrote {len(rows)} rows -> {args.out}")


if __name__ == "__main__":
    main()
