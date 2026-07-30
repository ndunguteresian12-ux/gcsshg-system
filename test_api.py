"""
Quick test script for the GCSSHG API - run this instead of typing curl
commands (curl's quoting/line-continuation rules are painful on Windows CMD).

Usage: python test_api.py
Make sure uvicorn is already running in another window before you run this.
"""

import requests

BASE = "http://127.0.0.1:8000"
session = requests.Session()  # keeps the login cookie between requests automatically

# 1. Log in - replace with the phone/password you created via create_officer.py
login_resp = session.post(f"{BASE}/auth/login", json={
    "phone": "0728850004",
    "password": "@Frank34785144",
})
print("LOGIN:", login_resp.status_code, login_resp.json())

# 2. Add a member
member_resp = session.post(f"{BASE}/members", json={
    "full_name": "Mrs Karanja",
    "phone": "0700111222",
})
print("CREATE MEMBER:", member_resp.status_code, member_resp.json())
member_id = member_resp.json()["id"]

# 3. Record a contribution
contrib_resp = session.post(f"{BASE}/contributions", json={
    "member_id": member_id,
    "contribution_month": "2026-03-01",
    "amount": 600,
})
print("CONTRIBUTION:", contrib_resp.status_code, contrib_resp.json())

# 4. Pull the statement
statement_resp = session.get(f"{BASE}/members/{member_id}/statement")
print("STATEMENT:", statement_resp.status_code, statement_resp.json())
