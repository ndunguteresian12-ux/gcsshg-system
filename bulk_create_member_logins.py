"""
Bulk-creates member logins from a CSV (see member_logins_template.csv).

Two modes per row:
- Leave `existing_officer_phone` blank -> creates a brand-new 'member' role
  account for that phone number, linked to their member record.
- Fill in `existing_officer_phone` (e.g. your treasurer's or secretary's own
  phone) -> links that EXISTING officer account to their member record
  instead of creating a duplicate login. They'll keep using their one
  officer login to see their own statement too.

Outputs a report (printed + saved to member_logins_report.csv) with the
setup link for each newly created account. Existing-officer links don't
need a new link - they already have their password.

Usage:
    python bulk_create_member_logins.py member_logins_template.csv
"""

import sys
import csv
import os
import itsdangerous
import psycopg2
import psycopg2.extras

from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
SECRET_KEY = os.environ.get("SESSION_SECRET", "change-me-in-production")
SITE_URL = os.environ.get("SITE_URL", "http://127.0.0.1:8000")
signer = itsdangerous.URLSafeTimedSerializer(SECRET_KEY)


def main():
    if len(sys.argv) != 2:
        print("Usage: python bulk_create_member_logins.py <csv_file>")
        return
    csv_path = sys.argv[1]

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    results = []  # (name, status, link_or_note)

    try:
        with conn.cursor() as cur, open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row["full_name"].strip()
                phone = (row.get("phone") or "").strip()
                existing_phone = (row.get("existing_officer_phone") or "").strip()
                if not name:
                    continue

                cur.execute("SELECT * FROM members WHERE full_name = %s", (name,))
                member = cur.fetchone()
                if not member:
                    results.append((name, "SKIPPED", "No matching member record found - check spelling."))
                    continue
                if member["user_id"]:
                    results.append((name, "SKIPPED", "Already has a login."))
                    continue

                if existing_phone:
                    cur.execute("SELECT id FROM users WHERE phone = %s", (existing_phone,))
                    officer = cur.fetchone()
                    if not officer:
                        results.append((name, "ERROR", f"No officer account found with phone {existing_phone}."))
                        continue
                    cur.execute("UPDATE members SET user_id = %s WHERE id = %s", (officer["id"], member["id"]))
                    results.append((name, "LINKED", f"Linked to existing officer account ({existing_phone}) - no new link needed."))
                    continue

                if not phone:
                    results.append((name, "SKIPPED", "No phone number given."))
                    continue
                cur.execute("SELECT id FROM users WHERE phone = %s", (phone,))
                if cur.fetchone():
                    results.append((name, "ERROR", f"Phone {phone} is already registered to another account."))
                    continue

                cur.execute(
                    "INSERT INTO users (phone, full_name, password_hash, role) "
                    "VALUES (%s, %s, NULL, 'member') RETURNING id",
                    (phone, name),
                )
                user_id = cur.fetchone()["id"]
                cur.execute("UPDATE members SET user_id = %s, phone = %s WHERE id = %s",
                            (user_id, phone, member["id"]))

                token = signer.dumps({"user_id": user_id, "purpose": "set_password"})
                link = f"{SITE_URL}/set-password?token={token}"
                results.append((name, "CREATED", link))

            conn.commit()
    finally:
        conn.close()

    print(f"\n{'NAME':<25}{'STATUS':<12}DETAIL")
    print("-" * 100)
    for name, status, detail in results:
        print(f"{name:<25}{status:<12}{detail}")

    with open("member_logins_report.csv", "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(["full_name", "status", "detail_or_link"])
        writer.writerows(results)
    print("\nFull report also saved to member_logins_report.csv")


if __name__ == "__main__":
    main()
