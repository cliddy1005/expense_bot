#!/usr/bin/env python3
"""
Append a row to expenses.csv in the format expensify_safari_existing_v32.py expects:
  report_url,date,merchant,amount,currency,category,description,receipt_path

Category defaults: TFL -> Travel, everything else -> Subsistence.
Skips (with a warning) if the receipt_path is already present in the CSV.
"""
import argparse
import csv
import sys
from pathlib import Path

DEFAULT_CSV = Path("~/Expenses/expenses.csv").expanduser()
DEFAULT_REPORT_URL = (
    "https://www.expensify.com/report?param="
    "{%22pageReportID%22:621446405922293,%22keepCollection%22:true}"
)
FIELDS = ["report_url", "date", "merchant", "amount", "currency",
          "category", "description", "receipt_path"]

SUBSISTENCE_PER_EXPENSE_CAP = 20.00
SUBSISTENCE_PER_DAY_CAP = 40.00


def auto_category(merchant: str) -> str:
    return "Travel" if merchant.strip().upper() == "TFL" else "Subsistence"


def subsistence_total_for_date(csv_path: Path, target_date: str) -> float:
    if not csv_path.is_file():
        return 0.0
    total = 0.0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("date") or "").strip() == target_date \
                    and (row.get("category") or "").strip() == "Subsistence":
                try:
                    total += float(row.get("amount") or 0)
                except ValueError:
                    pass
    return total


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", default=str(DEFAULT_CSV))
    p.add_argument("--report-url", default=DEFAULT_REPORT_URL)
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--merchant", required=True)
    p.add_argument("--amount", required=True, type=float)
    p.add_argument("--currency", default="GBP")
    p.add_argument("--category", default=None, help="defaults from merchant")
    p.add_argument("--description", default=None, help="defaults to merchant")
    p.add_argument("--receipt-path", required=True)
    args = p.parse_args()

    receipt_path = str(Path(args.receipt_path).expanduser().resolve())
    if not Path(receipt_path).is_file():
        sys.exit(f"ERROR: receipt file does not exist: {receipt_path}")

    csv_path = Path(args.csv).expanduser()
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    existing_receipts = set()
    file_exists = csv_path.is_file() and csv_path.stat().st_size > 0
    if file_exists:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                rp = (row.get("receipt_path") or "").strip()
                if rp:
                    existing_receipts.add(str(Path(rp).expanduser().resolve()))

    if receipt_path in existing_receipts:
        sys.exit(f"SKIP: {receipt_path} already present in {csv_path}")

    category = args.category or auto_category(args.merchant)
    amount = args.amount

    if category == "Subsistence":
        capped = min(amount, SUBSISTENCE_PER_EXPENSE_CAP)
        day_total_so_far = subsistence_total_for_date(csv_path, args.date)
        remaining = round(SUBSISTENCE_PER_DAY_CAP - day_total_so_far, 2)
        if remaining <= 0:
            sys.exit(
                f"ERROR: {args.date} already has £{day_total_so_far:.2f} of Subsistence "
                f"(day cap £{SUBSISTENCE_PER_DAY_CAP:.2f}) - cannot add another Subsistence expense"
            )
        final = min(capped, remaining)
        if final != amount:
            print(f"NOTE: Subsistence amount reduced from £{amount:.2f} to £{final:.2f} "
                  f"(per-expense cap £{SUBSISTENCE_PER_EXPENSE_CAP:.2f}, "
                  f"day remaining £{remaining:.2f})")
        amount = final

    row = {
        "report_url": args.report_url,
        "date": args.date,
        "merchant": args.merchant,
        "amount": f"{amount:.2f}",
        "currency": args.currency.upper(),
        "category": category,
        "description": args.description or args.merchant,
        "receipt_path": receipt_path,
    }

    with csv_path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    print(f"Added: {row['date']}  {row['merchant']}  £{row['amount']}  ({row['category']})  -> {csv_path}")


if __name__ == "__main__":
    main()
