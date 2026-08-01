"""
Creates an officer account (chairperson, treasurer, secretary) WITHOUT a
password - the officer sets their own password themselves via a one-time
link this script prints out. Send that link to them over WhatsApp/SMS.

Usage:
    python create_officer.py
"""

import os
import itsdangerous
import psycopg2
import psycopg2.extras

from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
SECRET_KEY = os.environ.get("SESSION_SECRET", "change-me-in-production")
SITE_URL = os.environ.get("SITE_URL", "http://127.0.0.1:8000")  # set to your Render URL for production links

signer = itsdangerous.URLSafeTimedSerializer(SECRET_KEY)


def main():
    print("Create an officer account for GCSSHG (chairperson, treasurer, or secretary)")
    phone = input("Phone number (login username, e.g. 0712345678): ").strip()
    full_name = input("Full name: ").strip()
    role = input("Role [chairperson/treasurer/secretary]: ").strip().lower()
    if role not in ("chairperson", "treasurer", "secretary"):
        print("Role must be chairperson, treasurer, or secretary.")
        return

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (phone, full_name, password_hash, role) "
                "VALUES (%s, %s, NULL, %s) RETURNING id",
                (phone, full_name, role),
            )
            user_id = cur.fetchone()["id"]
            conn.commit()
    finally:
        conn.close()

    token = signer.dumps({"user_id": user_id, "purpose": "set_password"})
    link = f"{SITE_URL}/set-password?token={token}"

    print(f"\nCreated {role} account for {full_name} (id={user_id}).")
    print("Send this one-time setup link to them (valid for 48 hours):\n")
    print(link)
    print()


if __name__ == "__main__":
    main()
