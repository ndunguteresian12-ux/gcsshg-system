"""
Reissues a password-setup link for an existing officer account - use this
if the original link was wrong (e.g. pointed at localhost) or expired.

Usage:
    python resend_invite.py
"""

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
    phone = input("Officer's phone number: ").strip()

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, full_name, role FROM users WHERE phone = %s", (phone,))
            user = cur.fetchone()
    finally:
        conn.close()

    if not user:
        print(f"No account found with phone {phone}.")
        return

    token = signer.dumps({"user_id": user["id"], "purpose": "set_password"})
    link = f"{SITE_URL}/set-password?token={token}"

    print(f"\n{user['full_name']} ({user['role']}) - new setup link (valid 48 hours):\n")
    print(link)
    print()


if __name__ == "__main__":
    main()
