"""
One-off script to create officer logins (chairperson, treasurer, secretary).
There's no public signup endpoint on purpose - only run this yourself,
from the server / your own machine with DATABASE_URL set.

Usage:
    python3 create_officer.py
    (it will prompt you for details)
"""

import os
import getpass
import psycopg2
import bcrypt

from dotenv import load_dotenv
load_dotenv()  # reads .env in this folder

DATABASE_URL = os.environ["DATABASE_URL"]  # must be set in .env before running


def main():
    print("Create an officer login for GCSSHG")
    phone = input("Phone number (login username, e.g. 0712345678): ").strip()
    full_name = input("Full name: ").strip()
    role = input("Role [chairperson/treasurer/secretary]: ").strip().lower()
    if role not in ("chairperson", "treasurer", "secretary"):
        print("Role must be chairperson, treasurer, or secretary.")
        return
    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords don't match.")
        return

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (phone, full_name, password_hash, role) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (phone, full_name, password_hash, role),
            )
            user_id = cur.fetchone()[0]
            conn.commit()
            print(f"Created user id={user_id}, phone={phone}, role={role}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
