"""
Diagnostic only - shows the length and first/last 3 characters of the
loaded SESSION_SECRET (never the full value), so you can compare it
against what's set in Render's Environment tab without pasting secrets
into chat.

Usage: python check_env.py
"""

import os
from dotenv import load_dotenv
load_dotenv()

def masked(value):
    if not value:
        return "(not set)"
    if len(value) <= 6:
        return f"(length={len(value)}, too short to mask safely - just confirm it matches)"
    return f"{value[:3]}...{value[-3:]} (length={len(value)})"

print("SESSION_SECRET:", masked(os.environ.get("SESSION_SECRET")))
print("SITE_URL:", os.environ.get("SITE_URL", "(not set - defaults to 127.0.0.1)"))
print("DATABASE_URL host:", os.environ.get("DATABASE_URL", "(not set)").split("@")[-1] if os.environ.get("DATABASE_URL") else "(not set)")
print("\nRunning from:", os.getcwd())
print(".env file found here:", os.path.exists(".env"))
