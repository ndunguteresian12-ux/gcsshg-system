"""
Bulk-imports members and their monthly contributions from a CSV
(see members_import_template.csv for the expected format).

Validates each row: if the monthly columns you fill in don't add up to
the reference_y_total you also filled in, it flags a warning instead of
silently importing a wrong number - fix the CSV and re-run.

Usage:
    python import_members.py members_import_template.csv
"""

import sys
import csv
from datetime import date
from decimal import Decimal, InvalidOperation

import psycopg2
from dotenv import load_dotenv
load_dotenv()
import os

DATABASE_URL = os.environ["DATABASE_URL"]

MONTH_COLUMNS = ["mar", "apr", "may", "jun", "jul", "aug", "sept", "oct", "nov", "dec", "jan2"]
# Maps CSV column name -> (calendar month number, calendar year offset)
# Fiscal year runs Mar (year Y) through Jan (year Y+1) - adjust if your cycle differs
MONTH_MAP = {
    "mar": (3, 0), "apr": (4, 0), "may": (5, 0), "jun": (6, 0), "jul": (7, 0),
    "aug": (8, 0), "sept": (9, 0), "oct": (10, 0), "nov": (11, 0), "dec": (12, 0),
    "jan2": (1, 1),
}


def parse_amount(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def main():
    if len(sys.argv) != 2:
        print("Usage: python import_members.py <csv_file>")
        return
    csv_path = sys.argv[1]

    fiscal_start_year = int(input("Fiscal year start (e.g. 2025 for the Mar-2025..Jan-2026 cycle): ").strip())

    conn = psycopg2.connect(DATABASE_URL)
    warnings = []
    imported_members = 0
    imported_contributions = 0

    try:
        with conn.cursor() as cur, open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row["full_name"].strip()
                if not name:
                    continue

                monthly_amounts = []
                row_sum = Decimal("0")
                for col in MONTH_COLUMNS:
                    amt = parse_amount(row.get(col, ""))
                    if amt is not None:
                        month_num, year_offset = MONTH_MAP[col]
                        contrib_date = date(fiscal_start_year + year_offset, month_num, 1)
                        monthly_amounts.append((contrib_date, amt))
                        row_sum += amt

                ref_total = parse_amount(row.get("reference_y_total", ""))
                if ref_total is not None and row_sum != ref_total:
                    warnings.append(
                        f"{name}: monthly entries sum to {row_sum} but reference_y_total says {ref_total} "
                        f"(difference of {ref_total - row_sum}) - check this row against the paper ledger."
                    )

                # create the member
                cur.execute(
                    "INSERT INTO members (full_name, join_date) VALUES (%s, %s) RETURNING id",
                    (name, date(fiscal_start_year, 3, 1)),
                )
                member_id = cur.fetchone()[0]
                imported_members += 1

                for contrib_date, amt in monthly_amounts:
                    cur.execute(
                        "INSERT INTO contributions (member_id, contribution_month, amount) VALUES (%s, %s, %s)",
                        (member_id, contrib_date, amt),
                    )
                    imported_contributions += 1

            conn.commit()
    finally:
        conn.close()

    print(f"\nImported {imported_members} members and {imported_contributions} contribution records.\n")
    if warnings:
        print(f"⚠ {len(warnings)} row(s) need a second look:")
        for w in warnings:
            print(" -", w)
    else:
        print("All rows with a reference total matched - looking good.")


if __name__ == "__main__":
    main()
