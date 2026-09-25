"""
GCSSHG (Githiga Comprehensive School Self Help Group) management system.

Same stack pattern as Elimu Hub: FastAPI + Postgres (RealDictCursor),
signed sessions for auth, deployable to Render with Neon Postgres.

This is an MVP scaffold covering the core workflow:
  - Chairperson/treasurer/secretary log in
  - Treasurer records monthly contributions per member
  - Treasurer issues loans and records repayments (tiered interest auto-applied)
  - At fiscal year-end, chairperson runs the dividend calculation
  - Every member can pull their own real-time statement (contributions,
    loans, dividends) - this is what the "mobile phone" view hits
"""

import os
import json
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List
from urllib.parse import urlencode, quote_plus
import requests

from dotenv import load_dotenv
load_dotenv()  # reads .env in this folder so DATABASE_URL/SESSION_SECRET don't need manual `set`

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Depends, Cookie, Response, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import bcrypt
import itsdangerous

from calculations import (
    classify_loan, loan_interest_due, loan_balance_due,
    run_dividends, MemberContributionRecord, fiscal_month_sequence, month_weight,
    run_dividends_cumulative, MemberContributionRecordCumulative, cumulative_month_weight,
    compute_due_date, is_loan_overdue, loan_overdue_months, loan_penalty_due,
    mid_tier_installment_schedule, LoanTerms, add_months,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/gcsshg")
SECRET_KEY = os.environ.get("SESSION_SECRET", "change-me-in-production")
SITE_URL = os.environ.get("SITE_URL", "http://127.0.0.1:8000")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", SMTP_USERNAME)
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "GCSSHG")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
REMINDER_SECRET = os.environ.get("REMINDER_SECRET", "")
signer = itsdangerous.URLSafeTimedSerializer(SECRET_KEY)

app = FastAPI(title="GCSSHG Management System")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/sw.js")
def service_worker():
    # Served from root (not /static/) so its default scope covers the
    # whole site - a service worker's max scope is the directory it's
    # served from, and browsers require a special header to widen that,
    # so serving it here avoids needing that entirely.
    return FileResponse("static/sw.js", media_type="application/javascript")


@app.get("/manifest.json")
def manifest():
    return FileResponse("static/manifest.json", media_type="application/manifest+json")


def get_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def get_settings(cur):
    cur.execute("SELECT * FROM group_settings ORDER BY id LIMIT 1")
    return cur.fetchone()


def compute_contribution_standing(cur, member_id, join_date, min_contribution, as_of):
    """
    Single source of truth for the cumulative contribution check - used by
    BOTH the penalty engine and the member statement page, so they can
    never disagree with each other.
    Returns months elapsed since joining (not counting the current
    in-progress month), what should have been contributed by now, what
    actually has been, and the shortfall (0 if caught up or ahead).
    """
    join_month = join_date.replace(day=1)
    current_month_start = as_of.replace(day=1)
    months_elapsed = 0
    cursor_month = join_month
    while cursor_month < current_month_start:
        months_elapsed += 1
        cursor_month = add_months(cursor_month, 1)

    expected_cumulative = min_contribution * months_elapsed
    cur.execute(
        "SELECT COALESCE(SUM(amount),0) as total FROM contributions WHERE member_id = %s",
        (member_id,),
    )
    actual_cumulative = cur.fetchone()["total"]
    shortfall = max(expected_cumulative - actual_cumulative, Decimal("0"))
    is_current = actual_cumulative >= expected_cumulative

    return {
        "months_elapsed": months_elapsed,
        "expected_cumulative": expected_cumulative,
        "actual_cumulative": actual_cumulative,
        "shortfall": shortfall,
        "is_current": is_current,
    }


def _pct_change(new, old):
    """Percent change from old to new. If old is 0, treat any positive new value as +100%."""
    if old == 0:
        return Decimal("100") if new > 0 else Decimal("0")
    return ((new - old) / old * 100).quantize(Decimal("0.1"))


def compute_contribution_growth(cur, today):
    """
    Monthly growth: the most recently COMPLETED month vs the one before it
    (comparing two full months, not a partial in-progress month).
    Annual growth: this calendar year so far vs the same Jan-through-this-month
    span last year (a fair year-over-year comparison partway through a year).
    """
    last_complete_month = add_months(today.replace(day=1), -1)
    month_before_that = add_months(last_complete_month, -1)

    cur.execute(
        "SELECT COALESCE(SUM(amount),0) as total FROM contributions WHERE contribution_month = %s",
        (last_complete_month,),
    )
    current_month_total = cur.fetchone()["total"]
    cur.execute(
        "SELECT COALESCE(SUM(amount),0) as total FROM contributions WHERE contribution_month = %s",
        (month_before_that,),
    )
    prior_month_total = cur.fetchone()["total"]
    monthly_pct = _pct_change(current_month_total, prior_month_total)

    cur.execute(
        "SELECT COALESCE(SUM(amount),0) as total FROM contributions WHERE EXTRACT(YEAR FROM contribution_month) = %s",
        (today.year,),
    )
    this_year_total = cur.fetchone()["total"]
    cur.execute(
        """SELECT COALESCE(SUM(amount),0) as total FROM contributions
           WHERE EXTRACT(YEAR FROM contribution_month) = %s AND EXTRACT(MONTH FROM contribution_month) <= %s""",
        (today.year - 1, today.month),
    )
    last_year_same_span_total = cur.fetchone()["total"]
    annual_pct = _pct_change(this_year_total, last_year_same_span_total)

    return {"monthly_pct": monthly_pct, "annual_pct": annual_pct}


def _total_expected_interest_as_of(all_loans, as_of_date):
    total = Decimal("0")
    for loan in all_loans:
        if loan["issue_date"] > as_of_date:
            continue  # loan didn't exist yet at that point in time
        terms = terms_from_loan_row(loan)
        override = loan.get("interest_override_periods")
        effective_as_of = as_of_date
        if loan["status"] == "cleared" and loan["cleared_date"] and loan["cleared_date"] < as_of_date:
            effective_as_of = loan["cleared_date"]
        total += loan_interest_due(loan["principal"], loan["issue_date"], effective_as_of, terms, override)
    return total


def compute_interest_growth(all_loans, today):
    """
    Compares total EXPECTED (accrued) interest across the whole portfolio,
    evaluated as of today vs one month ago vs one year ago, using the same
    calculator each time - so this is purely "how much has the interest
    calculator's total grown," not affected by collection timing.
    """
    one_month_ago = add_months(today, -1)
    one_year_ago = add_months(today, -12)

    expected_now = _total_expected_interest_as_of(all_loans, today)
    expected_month_ago = _total_expected_interest_as_of(all_loans, one_month_ago)
    expected_year_ago = _total_expected_interest_as_of(all_loans, one_year_ago)

    return {
        "monthly_pct": _pct_change(expected_now, expected_month_ago),
        "annual_pct": _pct_change(expected_now, expected_year_ago),
    }


def compute_timely_payment_pct(all_loans, today):
    """
    Of all loans with a due date, what fraction were (or are so far) paid on
    time: cleared on/before their due date, or still active and not yet
    overdue. Defaulted loans always count as not timely.
    """
    eligible = [l for l in all_loans if l["due_date"]]
    if not eligible:
        return None
    timely = 0
    for loan in eligible:
        if loan["status"] == "cleared":
            if loan["cleared_date"] and loan["cleared_date"] <= loan["due_date"]:
                timely += 1
        elif loan["status"] == "active":
            if today <= loan["due_date"]:
                timely += 1
        # 'defaulted' or an overdue active loan simply isn't counted as timely
    return (Decimal(timely) / Decimal(len(eligible)) * 100).quantize(Decimal("0.1"))


def send_email(to_email: str, subject: str, body_text: str) -> tuple:
    """
    Sends a plain-text email via Brevo's HTTP API (not raw SMTP - Render's
    free web services block outbound SMTP ports 25/465/587 entirely, but
    regular HTTPS, which this uses, is never blocked).
    Returns (success: bool, error_message: str|None). Never raises - a
    misconfigured or failed send should degrade gracefully rather than
    crash the request that triggered it. Every attempt is logged (visible
    in Render's log output) so a failure can actually be diagnosed.
    """
    if not BREVO_API_KEY:
        print("[EMAIL] Skipped - BREVO_API_KEY is not set.")
        return False, "Email is not configured yet - set BREVO_API_KEY."
    if not SMTP_FROM_EMAIL:
        print("[EMAIL] Skipped - no sender address configured (SMTP_FROM_EMAIL).")
        return False, "No sender email configured - set SMTP_FROM_EMAIL."
    if not to_email:
        print("[EMAIL] Skipped - no recipient address given.")
        return False, "No email address on file."
    try:
        resp = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "accept": "application/json",
                "api-key": BREVO_API_KEY,
                "content-type": "application/json",
            },
            json={
                "sender": {"name": SMTP_FROM_NAME, "email": SMTP_FROM_EMAIL},
                "to": [{"email": to_email}],
                "subject": subject,
                "textContent": body_text,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"[EMAIL] Sent OK to {to_email} - subject: {subject}")
            return True, None
        err = f"Brevo API error {resp.status_code}: {resp.text[:300]}"
        print(f"[EMAIL] FAILED to {to_email} - {err}")
        return False, err
    except Exception as e:
        print(f"[EMAIL] FAILED to {to_email} - subject: {subject} - error: {e!r}")
        return False, str(e)


LOAN_REMINDER_ADVANCE_DAYS = 3
CONTRIBUTION_REMINDER_DAY = 10  # of each month


def run_reminder_checks(cur, today):
    """
    Sends loan-due-date reminders (3 days before, and on the day itself)
    and a monthly contribution reminder (on the 10th, to anyone short of
    their minimum for the current month). Every send is logged in
    reminders_sent so re-running this on the same day never double-sends -
    safe to call repeatedly, whether from the daily cron trigger or a
    manual "test now" click.
    """
    settings = get_settings(cur)
    loan_reminders_sent = 0
    contribution_reminders_sent = 0

    # --- Loan due-date reminders ---
    cur.execute(
        """SELECT l.*, m.full_name, m.email FROM loans l
           JOIN members m ON m.id = l.member_id
           WHERE l.status = 'active' AND l.due_date IS NOT NULL AND m.email IS NOT NULL"""
    )
    active_loans = cur.fetchall()
    repaid_map = get_repaid_map(cur)
    for loan in active_loans:
        days_until_due = (loan["due_date"] - today).days
        if days_until_due not in (LOAN_REMINDER_ADVANCE_DAYS, 0):
            continue
        detail = build_loan_detail(cur, loan, today, settings["penalty_amount"], repaid_map)
        if detail["current_balance"] <= 0:
            continue
        reminder_type = "loan_due_today" if days_until_due == 0 else "loan_due_advance"
        period_label = str(loan["due_date"])
        cur.execute(
            """INSERT INTO reminders_sent (member_id, reminder_type, reference_id, period_label)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (member_id, reminder_type, reference_id, period_label) DO NOTHING
               RETURNING id""",
            (loan["member_id"], reminder_type, loan["id"], period_label),
        )
        if cur.fetchone():
            when = "is due today" if days_until_due == 0 else f"is due in {LOAN_REMINDER_ADVANCE_DAYS} days ({loan['due_date']})"
            body = (
                f"Hello {loan['full_name']},\n\n"
                f"This is a reminder that your loan of KES {loan['principal']:,.0f} {when}.\n"
                f"Current balance: KES {detail['current_balance']:,.2f}.\n\n"
                f"Please arrange repayment with your treasurer.\n\n- GCSSHG"
            )
            ok, _ = send_email(loan["email"], "GCSSHG loan payment reminder", body)
            if ok:
                loan_reminders_sent += 1

    # --- Monthly contribution reminder (10th of the month) ---
    if today.day == CONTRIBUTION_REMINDER_DAY:
        current_month = today.replace(day=1)
        cur.execute("SELECT id, full_name, email FROM members WHERE status = 'active' AND email IS NOT NULL")
        for m in cur.fetchall():
            cur.execute(
                "SELECT COALESCE(SUM(amount),0) as total FROM contributions "
                "WHERE member_id = %s AND contribution_month = %s",
                (m["id"], current_month),
            )
            total = cur.fetchone()["total"]
            if total >= settings["min_monthly_contribution"]:
                continue
            period_label = f"{today.year}-{today.month:02d}"
            cur.execute(
                """INSERT INTO reminders_sent (member_id, reminder_type, reference_id, period_label)
                   VALUES (%s, 'contribution_due', NULL, %s)
                   ON CONFLICT (member_id, reminder_type, reference_id, period_label) DO NOTHING
                   RETURNING id""",
                (m["id"], period_label),
            )
            if cur.fetchone():
                body = (
                    f"Hello {m['full_name']},\n\n"
                    f"This is a reminder that your GCSSHG contribution of at least "
                    f"KES {settings['min_monthly_contribution']:,.0f} for {current_month.strftime('%B %Y')} "
                    f"hasn't been recorded yet.\n\n"
                    f"Please make your contribution as soon as you can.\n\n- GCSSHG"
                )
                ok, _ = send_email(m["email"], "GCSSHG monthly contribution reminder", body)
                if ok:
                    contribution_reminders_sent += 1

    return loan_reminders_sent, contribution_reminders_sent


def time_greeting():
    """Simple time-of-day greeting, adjusted for East Africa Time (UTC+3)."""
    hour = (datetime.utcnow().hour + 3) % 24
    if hour < 12:
        return "Good morning"
    elif hour < 17:
        return "Good afternoon"
    return "Good evening"


def terms_from_loan_row(loan) -> LoanTerms:
    """
    Reconstructs a loan's ORIGINAL terms from what was stored at issuance,
    rather than re-deriving from current settings - so a later change to
    group-wide interest rates never retroactively changes an already-issued
    loan's interest calculation.
    """
    return LoanTerms(
        tier=loan["interest_tier"],
        rate_percent=Decimal(loan["interest_rate"]),
        period_months=loan["period_months"],
        deadline_months=0,  # unused here - due_date is already stored on the loan itself
    )


def get_repaid_map(cur):
    """One query for every loan's total repaid, instead of one query per loan."""
    cur.execute("SELECT loan_id, COALESCE(SUM(amount),0) as repaid FROM loan_repayments GROUP BY loan_id")
    return {row["loan_id"]: row["repaid"] for row in cur.fetchall()}


def build_loan_detail(cur, loan, as_of, penalty_amount, repaid_map=None):
    """
    Computes repaid amount, current balance, overdue status, and penalty
    owed for a single loan - used consistently everywhere a loan is displayed.
    Pass repaid_map (from get_repaid_map) when building details for many
    loans at once, to avoid a separate query per loan.
    """
    terms = terms_from_loan_row(loan)
    override = loan.get("interest_override_periods")
    if repaid_map is not None:
        repaid = repaid_map.get(loan["id"], Decimal("0"))
    else:
        cur.execute(
            "SELECT COALESCE(SUM(amount), 0) as repaid FROM loan_repayments WHERE loan_id = %s",
            (loan["id"],),
        )
        repaid = cur.fetchone()["repaid"]
    balance = loan_balance_due(loan["principal"], repaid, loan["issue_date"], as_of, terms, override)
    due = loan["due_date"]
    overdue = is_loan_overdue(due, balance, as_of) if due else False
    overdue_months = loan_overdue_months(due, as_of) if overdue else 0
    penalty = loan_penalty_due(due, balance, as_of, penalty_amount) if due else Decimal("0")
    return {
        **loan, "amount_repaid": repaid, "current_balance": balance,
        "is_overdue": overdue, "overdue_months": overdue_months, "penalty_due": penalty,
    }


def get_member_total_deposits(cur, member_id):
    cur.execute("SELECT COALESCE(SUM(amount),0) as total FROM contributions WHERE member_id = %s", (member_id,))
    return cur.fetchone()["total"]


def get_member_active_loan(cur, member_id):
    cur.execute("SELECT * FROM loans WHERE member_id = %s AND status = 'active'", (member_id,))
    return cur.fetchone()


def get_guarantor_committed_amount(cur, member_id, exclude_loan_id=None):
    """Sum of what this member is currently guaranteeing across other ACTIVE loans."""
    query = """SELECT COALESCE(SUM(lg.amount_guaranteed),0) as total FROM loan_guarantors lg
               JOIN loans l ON l.id = lg.loan_id
               WHERE lg.guarantor_member_id = %s AND l.status = 'active'"""
    params = [member_id]
    if exclude_loan_id:
        query += " AND lg.loan_id != %s"
        params.append(exclude_loan_id)
    cur.execute(query, params)
    return cur.fetchone()["total"]


def get_guarantor_available_capacity(cur, member_id, penalty_amount, exclude_loan_id=None):
    """
    A member's capacity to guarantee (part of) a loan: their own deposits,
    minus their own active loan's balance (if they have one - a guarantor
    whose own loan already exceeds their deposits has zero capacity, which
    is exactly the 'reject over-leveraged guarantors' rule), minus whatever
    they're already committed to guaranteeing elsewhere. Never negative.
    """
    deposits = get_member_total_deposits(cur, member_id)
    own_loan = get_member_active_loan(cur, member_id)
    own_loan_balance = Decimal("0")
    if own_loan:
        detail = build_loan_detail(cur, own_loan, date.today(), penalty_amount)
        own_loan_balance = detail["current_balance"]
    committed = get_guarantor_committed_amount(cur, member_id, exclude_loan_id)
    return max(deposits - own_loan_balance - committed, Decimal("0"))


def get_unpaid_loan_principal(cur):
    """
    Sum, across every ACTIVE or DEFAULTED loan, of principal not yet
    recovered (the principal portion only - excludes interest, since
    interest was never part of the contributed pool). Defaulted loans are
    included because that money is still genuinely unrecovered - it
    hasn't been formally written off, just unlikely to come back. A
    cleared loan is excluded since its principal has already returned to
    the bank. Used for the contribution reconciliation audit.
    """
    cur.execute(
        """SELECT COALESCE(SUM(l.principal - COALESCE(pr.repaid_principal, 0)), 0) as unpaid_principal
           FROM loans l
           LEFT JOIN (
               SELECT loan_id, SUM(principal_component) as repaid_principal
               FROM loan_repayments GROUP BY loan_id
           ) pr ON pr.loan_id = l.id
           WHERE l.status IN ('active', 'defaulted')"""
    )
    return cur.fetchone()["unpaid_principal"]


def get_current_bank_balance(cur):
    """
    The bank balance is a running ledger: deposits add, withdrawals
    subtract. This is the sum of everything ever recorded, not just the
    most recent entry.
    """
    cur.execute(
        """SELECT COALESCE(SUM(
               CASE WHEN transaction_type = 'withdrawal' THEN -amount ELSE amount END
           ), 0) as total FROM bank_balance_log"""
    )
    return cur.fetchone()["total"]


def get_payable_dividends(cur):
    """
    Interest actually collected in cash, minus dividends already paid out -
    the pool of money genuinely available to distribute that hasn't been
    yet. Same figure the dividends page uses to suggest a starting amount.
    """
    cur.execute("SELECT COALESCE(SUM(interest_component),0) as total FROM loan_repayments")
    collected = cur.fetchone()["total"]
    cur.execute("SELECT COALESCE(SUM(dividend_amount),0) as total FROM dividends")
    already_paid = cur.fetchone()["total"]
    return collected - already_paid


def compute_funds_reconciliation(cur):
    """
    The core check: money sitting in the bank plus loan principal not yet
    recovered should be AT LEAST what members have contributed.

        surplus = (bank balance + unpaid principal) - total contributions

    A surplus (positive) is healthy - it reflects interest earned that
    hasn't been paid out as dividends yet, not a problem. A deficit
    (negative) is the real red flag: assets are smaller than what members
    put in, meaning money that should exist doesn't - a missing
    contribution, an unlogged loan, or a bank balance entry that's wrong.

    This deliberately does NOT try to net interest, penalties, and
    dividends into the check itself to force it to exactly zero - those
    figures depend on inputs (penalties marked paid, an up-to-date bank
    balance) that aren't necessarily kept current, so baking them into a
    single "should be zero" number risks looking precise while being
    misleading. They're still shown for context: if the surplus looks
    smaller than expected given interest collected so far, that's usually
    a sign the bank balance entry is stale and needs rechecking against
    the real statement, not that money is missing.
    """
    cur.execute("SELECT COALESCE(SUM(amount),0) as total FROM contributions")
    total_contributions = cur.fetchone()["total"]

    unpaid_principal = get_unpaid_loan_principal(cur)
    bank_balance = get_current_bank_balance(cur)
    money_accounted_for = unpaid_principal + bank_balance
    surplus = money_accounted_for - total_contributions

    cur.execute("SELECT COALESCE(SUM(interest_component),0) as total FROM loan_repayments")
    interest_collected = cur.fetchone()["total"]

    cur.execute("SELECT COALESCE(SUM(amount),0) as total FROM penalties WHERE paid = TRUE")
    penalties_collected = cur.fetchone()["total"]

    cur.execute("SELECT COALESCE(SUM(dividend_amount),0) as total FROM dividends")
    dividends_paid = cur.fetchone()["total"]
    retained_interest_expected = interest_collected + penalties_collected - dividends_paid

    return {
        "total_contributions": total_contributions,
        "unpaid_principal": unpaid_principal,
        "bank_balance": bank_balance,
        "money_accounted_for": money_accounted_for,
        "surplus": surplus,
        "interest_collected": interest_collected,
        "penalties_collected": penalties_collected,
        "dividends_paid": dividends_paid,
        "retained_interest_expected": retained_interest_expected,
    }


# ---------------------------------------------------------------------------
# AUTH (signed session cookie, same approach as Elimu Hub's
# require_tenant_session / require_admin_session helpers)
# ---------------------------------------------------------------------------

def create_session_token(user_id: int, role: str) -> str:
    return signer.dumps({"user_id": user_id, "role": role})


def require_session(session: Optional[str] = Cookie(default=None)):
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in")
    try:
        data = signer.loads(session, max_age=60 * 60 * 24 * 14)  # 14 days
    except itsdangerous.BadSignature:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return data


def get_session_optional(session: Optional[str] = Cookie(default=None)):
    """Like require_session, but returns None instead of raising - used by
    page routes so we can redirect to /login instead of showing a JSON error."""
    if not session:
        return None
    try:
        return signer.loads(session, max_age=60 * 60 * 24 * 14)
    except itsdangerous.BadSignature:
        return None


def require_officer(session_data=Depends(require_session)):
    if session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        raise HTTPException(status_code=403, detail="Officers only")
    return session_data


def require_loan_officer(session_data=Depends(require_session)):
    """Chairperson and treasurer only - secretary can view loans but not transact on them."""
    if session_data["role"] not in ("chairperson", "treasurer"):
        raise HTTPException(status_code=403, detail="Chairperson or treasurer only")
    return session_data


def require_chairperson(session_data=Depends(require_session)):
    if session_data["role"] != "chairperson":
        raise HTTPException(status_code=403, detail="Chairperson only")
    return session_data


# ---------------------------------------------------------------------------
# SCHEMAS
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    phone: str
    password: str


class MemberCreate(BaseModel):
    full_name: str
    phone: Optional[str] = None
    join_date: Optional[date] = None


class ContributionCreate(BaseModel):
    member_id: int
    contribution_month: date  # pass as first-of-month, e.g. 2026-03-01
    amount: Decimal
    note: Optional[str] = None


class LoanCreate(BaseModel):
    member_id: int
    principal: Decimal
    issue_date: Optional[date] = None
    interest_override_periods: Optional[int] = None
    confirm_duplicate: bool = False


class RepaymentCreate(BaseModel):
    loan_id: int
    amount: Decimal
    payment_date: Optional[date] = None


class DividendRunRequest(BaseModel):
    fiscal_year_label: str
    fiscal_year_start_year: int
    total_interest_pool: Decimal


# ---------------------------------------------------------------------------
# AUTH ENDPOINTS
# ---------------------------------------------------------------------------

@app.post("/auth/login")
def login(payload: LoginRequest, response: Response):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE phone = %s AND is_active = TRUE", (payload.phone,))
            user = cur.fetchone()
            if not user or not bcrypt.checkpw(payload.password.encode(), user["password_hash"].encode()):
                raise HTTPException(status_code=401, detail="Invalid phone or password")
            token = create_session_token(user["id"], user["role"])
            response.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 14)
            return {"id": user["id"], "full_name": user["full_name"], "role": user["role"]}
    finally:
        conn.close()


@app.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


# ---------------------------------------------------------------------------
# MEMBERS
# ---------------------------------------------------------------------------

@app.post("/members")
def create_member(payload: MemberCreate, officer=Depends(require_chairperson)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO members (full_name, phone, join_date) VALUES (%s, %s, %s) RETURNING *",
                (payload.full_name, payload.phone, payload.join_date or date.today()),
            )
            member = cur.fetchone()
            conn.commit()
            return member
    finally:
        conn.close()


@app.get("/api/members")
def list_members(officer=Depends(require_officer)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM members ORDER BY full_name")
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CONTRIBUTIONS
# ---------------------------------------------------------------------------

@app.post("/contributions")
def record_contribution(payload: ContributionCreate, officer=Depends(require_officer)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            if payload.amount < settings["min_monthly_contribution"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Amount below group minimum of {settings['min_monthly_contribution']}",
                )
            contrib_month = payload.contribution_month.replace(day=1)
            cur.execute(
                """INSERT INTO contributions (member_id, contribution_month, amount, recorded_by, note)
                   VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                (payload.member_id, contrib_month, payload.amount, officer["user_id"], payload.note),
            )
            row = cur.fetchone()
            cur.execute(
                """INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
                   VALUES (%s, 'RECORD_CONTRIBUTION', 'contribution', %s, %s)""",
                (officer["user_id"], row["id"], psycopg2.extras.Json({"amount": str(payload.amount)})),
            )
            conn.commit()
            return row
    finally:
        conn.close()


@app.get("/members/{member_id}/statement")
def member_statement(member_id: int, session_data=Depends(require_session)):
    # a member may only view their own statement unless they're an officer
    if session_data["role"] == "member":
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM members WHERE user_id = %s", (session_data["user_id"],))
            own = cur.fetchone()
        conn.close()
        if not own or own["id"] != member_id:
            raise HTTPException(status_code=403, detail="Can only view your own statement")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM members WHERE id = %s", (member_id,))
            member = cur.fetchone()
            if not member:
                raise HTTPException(status_code=404, detail="Member not found")

            cur.execute(
                "SELECT contribution_month, amount, note FROM contributions "
                "WHERE member_id = %s ORDER BY contribution_month", (member_id,)
            )
            contributions = cur.fetchall()
            total_contributed = sum((c["amount"] for c in contributions), Decimal("0"))

            cur.execute("SELECT * FROM loans WHERE member_id = %s ORDER BY issue_date DESC", (member_id,))
            loans = cur.fetchall()
            settings = get_settings(cur)
            loan_details = [build_loan_detail(cur, loan, date.today(), settings["penalty_amount"]) for loan in loans]

            cur.execute(
                """SELECT d.dividend_amount, d.total_contribution, r.fiscal_year_label, r.computed_at
                   FROM dividends d JOIN dividend_runs r ON r.id = d.dividend_run_id
                   WHERE d.member_id = %s ORDER BY r.computed_at DESC""",
                (member_id,),
            )
            dividend_history = cur.fetchall()

            cur.execute(
                "SELECT * FROM penalties WHERE member_id = %s ORDER BY waived ASC, created_at DESC",
                (member_id,),
            )
            penalties = cur.fetchall()

            cur.execute("SELECT COALESCE(SUM(amount),0) as total FROM contributions")
            group_total_contributions = cur.fetchone()["total"]

            return {
                "member": member,
                "total_contributed": total_contributed,
                "contributions": contributions,
                "loans": loan_details,
                "dividend_history": dividend_history,
                "penalties": penalties,
                "group_total_contributions": group_total_contributions,
            }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# LOANS
# ---------------------------------------------------------------------------

@app.post("/loans")
def issue_loan(payload: LoanCreate, officer=Depends(require_loan_officer)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            issue_date = payload.issue_date or date.today()
            if not payload.confirm_duplicate:
                dup = find_duplicate_loan(cur, payload.member_id, payload.principal, issue_date)
                if dup:
                    raise HTTPException(
                        status_code=409,
                        detail=f"A loan of this exact amount and date already exists (loan #{dup['id']}). "
                               f"Set confirm_duplicate=true to issue anyway.",
                    )

            settings = get_settings(cur)
            terms = classify_loan(
                payload.principal,
                low_ceiling=settings["low_loan_ceiling"],
                low_rate=settings["low_loan_interest_rate"],
                mid_rate=settings["high_loan_interest_rate"],
                low_deadline=settings["low_loan_deadline_months"],
                mid_deadline=settings["high_loan_deadline_months"],
                high_ceiling=settings["high_loan_ceiling"],
            )
            due_date = compute_due_date(issue_date, terms)
            cur.execute(
                """INSERT INTO loans (member_id, principal, issue_date, due_date, interest_tier,
                                       interest_rate, period_months, approved_by, interest_override_periods)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (payload.member_id, payload.principal, issue_date, due_date,
                 terms.tier, terms.rate_percent, terms.period_months, officer["user_id"],
                 payload.interest_override_periods),
            )
            row = cur.fetchone()
            conn.commit()
            return row
    finally:
        conn.close()


@app.post("/loans/repayments")
def record_repayment(payload: RepaymentCreate, officer=Depends(require_loan_officer)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM loans WHERE id = %s", (payload.loan_id,))
            loan = cur.fetchone()
            if not loan:
                raise HTTPException(status_code=404, detail="Loan not found")
            terms = terms_from_loan_row(loan)
            override = loan.get("interest_override_periods")

            payment_date = payload.payment_date or date.today()
            interest_due = loan_interest_due(loan["principal"], loan["issue_date"], payment_date, terms, override)
            cur.execute(
                "SELECT COALESCE(SUM(interest_component),0) as paid_interest FROM loan_repayments WHERE loan_id = %s",
                (payload.loan_id,),
            )
            interest_already_paid = cur.fetchone()["paid_interest"]
            interest_outstanding = max(interest_due - interest_already_paid, Decimal("0"))

            interest_component = min(payload.amount, interest_outstanding)
            principal_component = payload.amount - interest_component

            cur.execute(
                """INSERT INTO loan_repayments (loan_id, payment_date, amount, principal_component,
                                                 interest_component, recorded_by)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                (payload.loan_id, payment_date, payload.amount, principal_component,
                 interest_component, officer["user_id"]),
            )
            repayment = cur.fetchone()

            cur.execute(
                "SELECT COALESCE(SUM(amount),0) as total_repaid FROM loan_repayments WHERE loan_id = %s",
                (payload.loan_id,),
            )
            total_repaid = cur.fetchone()["total_repaid"]
            full_balance = loan_balance_due(loan["principal"], total_repaid, loan["issue_date"], payment_date, terms, override)
            if full_balance <= 0:
                cur.execute("UPDATE loans SET status = 'cleared', cleared_date = %s WHERE id = %s",
                            (payment_date, payload.loan_id))
            conn.commit()
            return {"repayment": repayment, "remaining_balance": full_balance}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DIVIDENDS
# ---------------------------------------------------------------------------

def build_dividend_month_breakdown(cur, member_id, period_start, period_end, start_month, fiscal_length):
    """
    Month-by-month detail of exactly how one member's weighted contribution
    was built: every month of the fiscal year, in order, with what they
    contributed (0 if nothing), that month's weight, and the resulting
    weighted value - the clearest possible answer to "why did I get this
    dividend amount," since it's the same arithmetic run_dividends uses,
    just shown one line at a time instead of collapsed into one number.
    """
    cur.execute(
        "SELECT EXTRACT(MONTH FROM contribution_month)::int as mon, SUM(amount) as amt "
        "FROM contributions WHERE member_id = %s AND contribution_month >= %s AND contribution_month < %s "
        "GROUP BY mon",
        (member_id, period_start, period_end),
    )
    amounts_by_month = {r["mon"]: r["amt"] for r in cur.fetchall()}

    seq = fiscal_month_sequence(start_month, fiscal_length)
    rows = []
    total_contribution = Decimal("0")
    total_weighted = Decimal("0")
    cursor_date = period_start
    for i, cal_month in enumerate(seq):
        position = i + 1
        weight = month_weight(position, fiscal_length)
        amount = amounts_by_month.get(cal_month, Decimal("0"))
        weighted_value = amount * weight
        rows.append({
            "month_label": cursor_date.strftime("%b %Y"), "position": position,
            "weight": weight, "amount": amount, "weighted_value": weighted_value,
        })
        total_contribution += amount
        total_weighted += weighted_value
        cursor_date = add_months(cursor_date, 1)

    return {"rows": rows, "total_contribution": total_contribution, "total_weighted": total_weighted}


def build_dividend_month_breakdown_cumulative(cur, member_id, period_start, as_of_date):
    """
    Same idea as build_dividend_month_breakdown, but for a cumulative
    (since-inception) run: walks every month from the group's first
    contribution through as_of_date, spanning as many years as needed,
    with weight decreasing by 1 each month with no annual reset.
    """
    cur.execute(
        "SELECT contribution_month, amount FROM contributions "
        "WHERE member_id = %s AND contribution_month <= %s ORDER BY contribution_month",
        (member_id, as_of_date),
    )
    amounts_by_month = {r["contribution_month"]: r["amount"] for r in cur.fetchall()}

    rows = []
    total_contribution = Decimal("0")
    total_weighted = Decimal("0")
    cursor_date = period_start.replace(day=1)
    as_of_month_start = as_of_date.replace(day=1)
    while cursor_date <= as_of_month_start:
        weight = cumulative_month_weight(cursor_date.year, cursor_date.month, as_of_date.year, as_of_date.month)
        amount = amounts_by_month.get(cursor_date, Decimal("0"))
        weighted_value = amount * weight
        rows.append({
            "month_label": cursor_date.strftime("%b %Y"), "weight": weight,
            "amount": amount, "weighted_value": weighted_value,
        })
        total_contribution += amount
        total_weighted += weighted_value
        cursor_date = add_months(cursor_date, 1)

    return {"rows": rows, "total_contribution": total_contribution, "total_weighted": total_weighted}


def compute_dividend_records(cur, period_start, period_end):
    """
    period_start (inclusive) to period_end (exclusive) - the exact fiscal
    year window being paid out. Grouping by calendar month is only safe
    once contributions are scoped to a single fiscal year like this -
    without a date range, a March from one year and a March from another
    would merge into one bucket, silently double-counting prior years on
    any second or later dividend run.
    """
    cur.execute("SELECT id, full_name FROM members WHERE status = 'active'")
    members = cur.fetchall()
    records = []
    for m in members:
        cur.execute(
            "SELECT EXTRACT(MONTH FROM contribution_month)::int as mon, SUM(amount) as amt "
            "FROM contributions WHERE member_id = %s AND contribution_month >= %s AND contribution_month < %s "
            "GROUP BY mon",
            (m["id"], period_start, period_end),
        )
        rows = cur.fetchall()
        monthly = [(r["mon"], r["amt"]) for r in rows]
        records.append(MemberContributionRecord(member_id=m["id"], full_name=m["full_name"],
                                                  monthly_amounts=monthly))
    return records


def compute_dividend_records_cumulative(cur, as_of_date):
    """
    ALL contributions ever made, from the group's very first one through
    as_of_date - for the one-time cumulative dividend covering everything
    since inception, weighted by total elapsed months rather than
    position within one fiscal year.
    """
    cur.execute("SELECT id, full_name FROM members WHERE status = 'active'")
    members = cur.fetchall()
    records = []
    for m in members:
        cur.execute(
            "SELECT contribution_month, SUM(amount) as amt FROM contributions "
            "WHERE member_id = %s AND contribution_month <= %s GROUP BY contribution_month",
            (m["id"], as_of_date),
        )
        rows = cur.fetchall()
        monthly = [(r["contribution_month"], r["amt"]) for r in rows]
        records.append(MemberContributionRecordCumulative(member_id=m["id"], full_name=m["full_name"],
                                                            monthly_amounts=monthly))
    return records


def save_dividend_run(cur, fiscal_year_label, total_interest_pool, computed_by, results,
                       gross_pool=None, admin_amount=Decimal("0"), transaction_cost_amount=Decimal("0"),
                       admin_label="Administration", transaction_label="Transaction costs",
                       period_start=None, period_end=None, is_cumulative=False):
    total_weighted = sum((r.weighted_contribution for r in results), Decimal("0"))
    cur.execute(
        """INSERT INTO dividend_runs (fiscal_year_label, total_interest_pool,
                                       total_weighted_contributions, computed_by,
                                       gross_pool, admin_amount, transaction_cost_amount,
                                       admin_label, transaction_label, period_start, period_end,
                                       is_cumulative)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (fiscal_year_label, total_interest_pool, total_weighted, computed_by,
         gross_pool, admin_amount, transaction_cost_amount, admin_label, transaction_label,
         period_start, period_end, is_cumulative),
    )
    run_id = cur.fetchone()["id"]
    for r in results:
        cur.execute(
            """INSERT INTO dividends (dividend_run_id, member_id, total_contribution,
                                       weighted_contribution, dividend_amount)
               VALUES (%s, %s, %s, %s, %s)""",
            (run_id, r.member_id, r.total_contribution, r.weighted_contribution, r.dividend_amount),
        )
    return run_id


@app.post("/dividends/run")
def run_dividend_calculation(payload: DividendRunRequest, chair=Depends(require_chairperson)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            period_start = date(payload.fiscal_year_start_year, settings["fiscal_year_start_month"], 1)
            period_end = add_months(period_start, settings["fiscal_year_length_months"])
            records = compute_dividend_records(cur, period_start, period_end)
            results = run_dividends(
                records, payload.total_interest_pool,
                settings["fiscal_year_start_month"], settings["fiscal_year_length_months"],
            )
            run_id = save_dividend_run(cur, payload.fiscal_year_label, payload.total_interest_pool,
                                        chair["user_id"], results,
                                        period_start=period_start, period_end=period_end)
            conn.commit()
            return {
                "dividend_run_id": run_id,
                "results": [r.__dict__ for r in results],
            }
    finally:
        conn.close()


@app.get("/dividends", response_class=HTMLResponse)
def dividends_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+run+dividends", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM dividend_runs ORDER BY computed_at DESC")
            runs = cur.fetchall()
            suggested_pool = get_payable_dividends(cur)
    finally:
        conn.close()

    # Group runs sharing the same fiscal_year_label together - several
    # historical entries (or a historical entry plus a computed run) for
    # the same year show as one combined line, not scattered separately.
    grouped = {}
    for r in runs:
        label = r["fiscal_year_label"]
        if label not in grouped:
            grouped[label] = {"label": label, "run_count": 0, "total_pool": Decimal("0"),
                               "latest": r["computed_at"], "any_run_id": r["id"]}
        grouped[label]["run_count"] += 1
        grouped[label]["total_pool"] += r["total_interest_pool"]
        if r["computed_at"] > grouped[label]["latest"]:
            grouped[label]["latest"] = r["computed_at"]
    year_groups = sorted(grouped.values(), key=lambda g: g["latest"], reverse=True)

    return templates.TemplateResponse(request, "dividends.html", {
        "year_groups": year_groups, "suggested_pool": suggested_pool, "role": session_data["role"],
        "current_year": date.today().year,
    })


@app.get("/dividends/record-historical", response_class=HTMLResponse)
def record_historical_dividend_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+do+this", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT m.id, m.full_name, COALESCE(SUM(c.amount),0) as total_contributed
                   FROM members m LEFT JOIN contributions c ON c.member_id = m.id
                   WHERE m.status = 'active' GROUP BY m.id ORDER BY m.full_name"""
            )
            members = cur.fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "dividend_historical.html", {
        "members": members, "role": session_data["role"],
    })


@app.post("/dividends/record-historical")
async def record_historical_dividend_submit(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+do+this", status_code=303)
    form = await request.form()
    fiscal_year_label = form.get("fiscal_year_label", "").strip()
    if not fiscal_year_label:
        return RedirectResponse(url="/dividends/record-historical?error=Label+is+required", status_code=303)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            total_pool = Decimal("0")
            member_amounts = []
            for key, value in form.multi_items():
                if key.startswith("amount_") and value.strip():
                    member_id = int(key.replace("amount_", ""))
                    amount = Decimal(value)
                    if amount > 0:
                        member_amounts.append((member_id, amount))
                        total_pool += amount

            if not member_amounts:
                return RedirectResponse(
                    url="/dividends/record-historical?error=Enter+at+least+one+member's+amount", status_code=303
                )

            cur.execute(
                """INSERT INTO dividend_runs (fiscal_year_label, total_interest_pool,
                                               total_weighted_contributions, computed_by, notes)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (fiscal_year_label, total_pool, 0, session_data["user_id"],
                 "Historical payout - entered manually, pre-dates this system"),
            )
            run_id = cur.fetchone()["id"]

            for member_id, amount in member_amounts:
                cur.execute("SELECT COALESCE(SUM(amount),0) as total FROM contributions WHERE member_id = %s",
                            (member_id,))
                total_contribution = cur.fetchone()["total"]
                cur.execute(
                    """INSERT INTO dividends (dividend_run_id, member_id, total_contribution,
                                               weighted_contribution, dividend_amount)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (run_id, member_id, total_contribution, 0, amount),
                )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/dividends/{run_id}", status_code=303)


@app.post("/dividends/preview")
def dividends_preview(request: Request, fiscal_year_label: str = Form(...),
                       fiscal_year_start_year: Optional[int] = Form(None),
                       cumulative: Optional[str] = Form(None),
                       total_interest_pool: float = Form(...),
                       session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+run+dividends", status_code=303)
    is_cumulative = bool(cumulative)
    if not is_cumulative and not fiscal_year_start_year:
        return RedirectResponse(url="/dividends?error=Pick+a+fiscal+year+start+year+or+choose+cumulative", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            gross = Decimal(str(total_interest_pool))
            admin_amount = (gross * settings["dividend_admin_percent"] / 100).quantize(Decimal("0.01"))
            transaction_amount = (gross * settings["dividend_transaction_percent"] / 100).quantize(Decimal("0.01"))
            member_pool = gross - admin_amount - transaction_amount

            if is_cumulative:
                as_of_date = date.today()
                cur.execute("SELECT MIN(contribution_month) as earliest FROM contributions")
                period_start = cur.fetchone()["earliest"] or as_of_date
                period_end = as_of_date
                records = compute_dividend_records_cumulative(cur, as_of_date)
                results = run_dividends_cumulative(records, member_pool, as_of_date)
            else:
                period_start = date(fiscal_year_start_year, settings["fiscal_year_start_month"], 1)
                period_end = add_months(period_start, settings["fiscal_year_length_months"])
                records = compute_dividend_records(cur, period_start, period_end)
                results = run_dividends(
                    records, member_pool,
                    settings["fiscal_year_start_month"], settings["fiscal_year_length_months"],
                )
    finally:
        conn.close()
    results_sorted = sorted(results, key=lambda r: r.dividend_amount, reverse=True)
    return templates.TemplateResponse(request, "dividend_preview.html", {
        "results": results_sorted, "fiscal_year_label": fiscal_year_label,
        "fiscal_year_start_year": fiscal_year_start_year, "is_cumulative": is_cumulative,
        "period_start": period_start, "period_end": period_end,
        "total_interest_pool": total_interest_pool, "role": session_data["role"],
        "gross_pool": gross, "admin_amount": admin_amount,
        "transaction_amount": transaction_amount, "member_pool": member_pool,
        "admin_percent": settings["dividend_admin_percent"],
        "transaction_percent": settings["dividend_transaction_percent"],
        "admin_label": settings["dividend_admin_label"],
        "transaction_label": settings["dividend_transaction_label"],
    })


@app.post("/dividends/confirm")
def dividends_confirm(fiscal_year_label: str = Form(...), fiscal_year_start_year: Optional[int] = Form(None),
                       cumulative: Optional[str] = Form(None),
                       total_interest_pool: float = Form(...),
                       session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+run+dividends", status_code=303)
    is_cumulative = bool(cumulative)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            gross = Decimal(str(total_interest_pool))
            admin_amount = (gross * settings["dividend_admin_percent"] / 100).quantize(Decimal("0.01"))
            transaction_amount = (gross * settings["dividend_transaction_percent"] / 100).quantize(Decimal("0.01"))
            member_pool = gross - admin_amount - transaction_amount

            if is_cumulative:
                as_of_date = date.today()
                cur.execute("SELECT MIN(contribution_month) as earliest FROM contributions")
                period_start = cur.fetchone()["earliest"] or as_of_date
                period_end = as_of_date
                records = compute_dividend_records_cumulative(cur, as_of_date)
                results = run_dividends_cumulative(records, member_pool, as_of_date)
            else:
                period_start = date(fiscal_year_start_year, settings["fiscal_year_start_month"], 1)
                period_end = add_months(period_start, settings["fiscal_year_length_months"])
                records = compute_dividend_records(cur, period_start, period_end)
                results = run_dividends(
                    records, member_pool,
                    settings["fiscal_year_start_month"], settings["fiscal_year_length_months"],
                )
            run_id = save_dividend_run(cur, fiscal_year_label, member_pool,
                                        session_data["user_id"], results,
                                        gross_pool=gross, admin_amount=admin_amount,
                                        transaction_cost_amount=transaction_amount,
                                        admin_label=settings["dividend_admin_label"],
                                        transaction_label=settings["dividend_transaction_label"],
                                        period_start=period_start, period_end=period_end,
                                        is_cumulative=is_cumulative)
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/dividends/{run_id}", status_code=303)


@app.get("/dividends/year-sheet/{run_id}", response_class=HTMLResponse)
def dividend_year_sheet(run_id: int, request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+view+this", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT fiscal_year_label FROM dividend_runs WHERE id = %s", (run_id,))
            anchor = cur.fetchone()
            if not anchor:
                return HTMLResponse("<p style='font-family:sans-serif;padding:2rem'>Dividend run not found.</p>")
            label = anchor["fiscal_year_label"]

            cur.execute("SELECT * FROM dividend_runs WHERE fiscal_year_label = %s ORDER BY computed_at", (label,))
            runs = cur.fetchall()
            run_ids = [r["id"] for r in runs]

            # One row per member, dividend amounts combined across every run
            # sharing this fiscal year label.
            cur.execute(
                """SELECT m.id as member_id, m.full_name, COALESCE(SUM(d.dividend_amount),0) as total_dividend
                   FROM members m JOIN dividends d ON d.member_id = m.id
                   WHERE d.dividend_run_id = ANY(%s)
                   GROUP BY m.id, m.full_name ORDER BY total_dividend DESC""",
                (run_ids,),
            )
            member_totals = cur.fetchall()
            grand_total = sum((m["total_dividend"] for m in member_totals), Decimal("0"))
            total_pool = sum((r["total_interest_pool"] for r in runs), Decimal("0"))
    finally:
        conn.close()
    return templates.TemplateResponse(request, "dividend_year_sheet.html", {
        "label": label, "runs": runs, "member_totals": member_totals,
        "grand_total": grand_total, "total_pool": total_pool, "role": session_data["role"],
    })


@app.get("/dividends/{run_id}", response_class=HTMLResponse)
def dividend_run_detail(run_id: int, request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+view+this", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM dividend_runs WHERE id = %s", (run_id,))
            run = cur.fetchone()
            if not run:
                return HTMLResponse("<p style='font-family:sans-serif;padding:2rem'>Run not found.</p>")
            cur.execute(
                """SELECT d.*, m.full_name FROM dividends d JOIN members m ON m.id = d.member_id
                   WHERE d.dividend_run_id = %s ORDER BY d.dividend_amount DESC""",
                (run_id,),
            )
            dividends = cur.fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "dividend_run_detail.html", {
        "run": run, "dividends": dividends, "role": session_data["role"],
    })


@app.get("/dividends/{run_id}/member/{member_id}", response_class=HTMLResponse)
def dividend_member_breakdown(run_id: int, member_id: int, request: Request,
                               session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM dividend_runs WHERE id = %s", (run_id,))
            run = cur.fetchone()
            if not run:
                return HTMLResponse("<p style='font-family:sans-serif;padding:2rem'>Run not found.</p>")
            if not run["period_start"] or not run["period_end"]:
                return HTMLResponse(
                    "<p style='font-family:sans-serif;padding:2rem'>"
                    "This run was saved before month-by-month breakdowns were tracked, so the exact "
                    "period it used isn't on record. Its total and weighted contribution figures are "
                    "still shown on the run's own page."
                    "</p>"
                )
            cur.execute(
                """SELECT d.*, m.full_name FROM dividends d JOIN members m ON m.id = d.member_id
                   WHERE d.dividend_run_id = %s AND d.member_id = %s""",
                (run_id, member_id),
            )
            dividend = cur.fetchone()
            if not dividend:
                return HTMLResponse("<p style='font-family:sans-serif;padding:2rem'>No dividend record found for this member on this run.</p>")

            settings = get_settings(cur)
            if run["is_cumulative"]:
                breakdown = build_dividend_month_breakdown_cumulative(
                    cur, member_id, run["period_start"], run["period_end"]
                )
            else:
                breakdown = build_dividend_month_breakdown(
                    cur, member_id, run["period_start"], run["period_end"],
                    settings["fiscal_year_start_month"], settings["fiscal_year_length_months"],
                )
    finally:
        conn.close()
    return templates.TemplateResponse(request, "dividend_member_breakdown.html", {
        "run": run, "dividend": dividend, "breakdown": breakdown, "role": session_data["role"],
    })


@app.get("/bank-balance", response_class=HTMLResponse)
def bank_balance_page(request: Request, prefill_amount: Optional[str] = None,
                       prefill_note: Optional[str] = None, prefill_type: Optional[str] = None,
                       session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT b.*, u.full_name as recorded_by_name FROM bank_balance_log b
                   LEFT JOIN users u ON u.id = b.recorded_by ORDER BY b.recorded_at ASC"""
            )
            oldest_first = cur.fetchall()
            running = Decimal("0")
            for h in oldest_first:
                running += -h["amount"] if h["transaction_type"] == "withdrawal" else h["amount"]
                h["running_balance"] = running
            history = list(reversed(oldest_first))
            current_balance = running
    finally:
        conn.close()
    return templates.TemplateResponse(request, "bank_balance.html", {
        "history": history, "current_balance": current_balance, "role": session_data["role"],
        "prefill_amount": prefill_amount or "", "prefill_note": prefill_note or "",
        "prefill_type": prefill_type or "deposit",
    })


@app.post("/bank-balance/update")
def bank_balance_update(amount: float = Form(...), transaction_type: str = Form("deposit"),
                         note: str = Form(""), session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer"):
        return RedirectResponse(url="/bank-balance?error=Only+the+chairperson+or+treasurer+can+update+this", status_code=303)
    if transaction_type not in ("deposit", "withdrawal"):
        transaction_type = "deposit"
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bank_balance_log (amount, transaction_type, note, recorded_by) VALUES (%s, %s, %s, %s)",
                (amount, transaction_type, note or None, session_data["user_id"]),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/bank-balance", status_code=303)


@app.post("/bank-balance/{entry_id}/delete")
def bank_balance_delete(entry_id: int, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/bank-balance?error=Only+the+chairperson+can+delete+a+bank+balance+entry", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bank_balance_log WHERE id = %s", (entry_id,))
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/bank-balance?error=Entry+deleted", status_code=303)


@app.get("/send-email", response_class=HTMLResponse)
def send_email_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, full_name, email FROM members WHERE status = 'active' ORDER BY full_name"
            )
            members = cur.fetchall()
    finally:
        conn.close()
    email_configured = bool(BREVO_API_KEY and SMTP_FROM_EMAIL)
    return templates.TemplateResponse(request, "send_email.html", {
        "members": members, "role": session_data["role"], "email_configured": email_configured,
    })


@app.post("/send-email")
async def send_email_submit(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    form = await request.form()
    subject = form.get("subject", "").strip()
    body = form.get("body", "").strip()
    send_to_all = form.get("send_to_all") == "yes"
    selected_ids = [int(v) for k, v in form.multi_items() if k == "member_ids"]

    if not subject or not body:
        return RedirectResponse(url="/send-email?error=Subject+and+message+are+both+required", status_code=303)

    conn = get_conn()
    sent, skipped_no_email, failures = 0, 0, []
    try:
        with conn.cursor() as cur:
            if send_to_all:
                cur.execute("SELECT id, full_name, email FROM members WHERE status = 'active'")
                recipients = cur.fetchall()
            elif selected_ids:
                cur.execute(
                    "SELECT id, full_name, email FROM members WHERE id = ANY(%s)", (selected_ids,)
                )
                recipients = cur.fetchall()
            else:
                return RedirectResponse(url="/send-email?error=Pick+at+least+one+member+or+send+to+all", status_code=303)

            for m in recipients:
                if not m["email"]:
                    skipped_no_email += 1
                    continue
                personalized_body = f"Dear {m['full_name']},\n\n{body}\n\n- GCSSHG"
                ok, err = send_email(m["email"], subject, personalized_body)
                if ok:
                    sent += 1
                else:
                    failures.append(f"{m['full_name']}: {err}")
    finally:
        conn.close()

    message = f"Sent to {sent} member(s)"
    if skipped_no_email:
        message += f" - {skipped_no_email} skipped (no email on file)"
    if failures:
        message += f" - failed: {'; '.join(failures[:3])}"
        if len(failures) > 3:
            message += f" (+{len(failures) - 3} more)"
    return RedirectResponse(url=f"/send-email?{urlencode({'success': message})}", status_code=303)


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, full_name FROM members WHERE status = 'active' ORDER BY full_name")
            members = cur.fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "reports.html", {"role": session_data["role"], "members": members})


def build_member_ledger(cur, member_id):
    """Shared by the ledger page and the email-this-ledger route, so both always agree."""
    cur.execute("SELECT * FROM members WHERE id = %s", (member_id,))
    member = cur.fetchone()
    if not member:
        return None, []

    entries = []
    cur.execute(
        "SELECT contribution_month, amount, recorded_at FROM contributions WHERE member_id = %s",
        (member_id,),
    )
    for c in cur.fetchall():
        entries.append({"date": c["contribution_month"], "type": "Contribution", "detail": "", "amount": c["amount"]})

    cur.execute("SELECT * FROM loans WHERE member_id = %s", (member_id,))
    for l in cur.fetchall():
        entries.append({
            "date": l["issue_date"], "type": "Loan issued",
            "detail": f"Loan #{l['id']} - {l['interest_rate']}%/{l['period_months']}mo", "amount": l["principal"],
        })
        cur.execute("SELECT payment_date, amount FROM loan_repayments WHERE loan_id = %s", (l["id"],))
        for r in cur.fetchall():
            entries.append({"date": r["payment_date"], "type": "Loan repayment", "detail": f"Loan #{l['id']}", "amount": r["amount"]})

    cur.execute(
        "SELECT penalty_type, period_label, amount, waived, paid, created_at FROM penalties WHERE member_id = %s",
        (member_id,),
    )
    for p in cur.fetchall():
        status = "waived" if p["waived"] else ("paid" if p["paid"] else "owed")
        entries.append({
            "date": p["created_at"].date(), "type": "Penalty",
            "detail": f"{p['penalty_type'].replace('_', ' ')} ({p['period_label']}) - {status}", "amount": p["amount"],
        })

    cur.execute(
        """SELECT d.dividend_amount, r.fiscal_year_label, r.computed_at FROM dividends d
           JOIN dividend_runs r ON r.id = d.dividend_run_id WHERE d.member_id = %s""",
        (member_id,),
    )
    for d in cur.fetchall():
        entries.append({"date": d["computed_at"].date(), "type": "Dividend", "detail": d["fiscal_year_label"], "amount": d["dividend_amount"]})

    entries.sort(key=lambda e: e["date"])
    return member, entries


def render_ledger_as_text(member, entries):
    lines = [
        f"GCSSHG - Full transaction ledger for {member['full_name']}",
        f"Member since {member['join_date']}",
        "",
        f"{'Date':<12} {'Type':<16} {'Detail':<35} {'Amount':>12}",
        "-" * 78,
    ]
    for e in entries:
        lines.append(f"{e['date'].strftime('%d %b %Y'):<12} {e['type']:<16} {e['detail'][:35]:<35} {e['amount']:>12,.2f}")
    return "\n".join(lines)


def build_member_statement_data(cur, member_id, today):
    """
    Same data a member's own /statement page shows - used both there and
    by the email-this-statement route, so the two can never disagree.
    """
    cur.execute("SELECT * FROM members WHERE id = %s", (member_id,))
    member = cur.fetchone()
    if not member:
        return None

    settings = get_settings(cur)
    cur.execute(
        "SELECT contribution_month, amount FROM contributions WHERE member_id = %s ORDER BY contribution_month",
        (member_id,),
    )
    contributions = cur.fetchall()
    total = sum((c["amount"] for c in contributions), Decimal("0"))

    cur.execute("SELECT * FROM loans WHERE member_id = %s ORDER BY issue_date DESC", (member_id,))
    loans = cur.fetchall()
    repaid_map = get_repaid_map(cur)
    loan_details = [build_loan_detail(cur, l, today, settings["penalty_amount"], repaid_map) for l in loans]

    cur.execute(
        "SELECT * FROM penalties WHERE member_id = %s ORDER BY waived ASC, created_at DESC", (member_id,)
    )
    penalties = cur.fetchall()
    penalties_owed = sum((p["amount"] for p in penalties if not p["waived"] and not p["paid"]), Decimal("0"))

    cur.execute(
        """SELECT d.dividend_amount, d.total_contribution, r.fiscal_year_label, r.computed_at
           FROM dividends d JOIN dividend_runs r ON r.id = d.dividend_run_id
           WHERE d.member_id = %s ORDER BY r.computed_at DESC""",
        (member_id,),
    )
    dividends = cur.fetchall()

    cur.execute("SELECT COALESCE(SUM(amount),0) as total FROM contributions")
    group_total_contributions = cur.fetchone()["total"]

    standing = compute_contribution_standing(cur, member_id, member["join_date"], settings["min_monthly_contribution"], today)

    return {
        "member": member, "contributions": contributions, "total": total,
        "loans": loan_details, "penalties": penalties, "penalties_owed": penalties_owed,
        "dividends": dividends, "group_total_contributions": group_total_contributions,
        "standing": standing,
    }


def render_statement_as_text(data):
    m = data["member"]
    lines = [
        f"GCSSHG - Statement for {m['full_name']}",
        f"Member since {m['join_date']}",
        "",
        f"Your total contributed: KES {data['total']:,.2f}",
        f"Group's total contributions: KES {data['group_total_contributions']:,.2f}",
        "",
        "CONTRIBUTION STANDING",
    ]
    s = data["standing"]
    lines.append(f"  {s['months_elapsed']} month(s) elapsed x minimum = KES {s['expected_cumulative']:,.2f} expected by now")
    lines.append(f"  Actual contributed: KES {s['actual_cumulative']:,.2f}")
    lines.append("  Caught up - thank you!" if s["is_current"] else f"  Behind by KES {s['shortfall']:,.2f}")

    lines += ["", "CONTRIBUTION HISTORY"]
    if data["contributions"]:
        for c in data["contributions"]:
            lines.append(f"  {c['contribution_month'].strftime('%b %Y')}: KES {c['amount']:,.2f}")
    else:
        lines.append("  (none recorded yet)")

    lines += ["", "LOANS"]
    if data["loans"]:
        for l in data["loans"]:
            status = "overdue" if l["is_overdue"] else l["status"]
            lines.append(
                f"  Issued {l['issue_date']} - Principal KES {l['principal']:,.0f} - "
                f"Paid KES {l['amount_repaid']:,.2f} - Balance KES {l['current_balance']:,.2f} - {status}"
            )
    else:
        lines.append("  (none on record)")

    lines += ["", "PENALTIES"]
    lines.append(f"  Total outstanding: KES {data['penalties_owed']:,.2f}")
    if data["penalties"]:
        for p in data["penalties"]:
            status = "paid" if p["paid"] else ("waived" if p["waived"] else "owed")
            lines.append(f"  {p['period_label']} ({p['penalty_type'].replace('_',' ')}): KES {p['amount']:,.2f} - {status}")
    else:
        lines.append("  (none on record)")

    lines += ["", "DIVIDEND HISTORY"]
    if data["dividends"]:
        for d in data["dividends"]:
            lines.append(f"  {d['fiscal_year_label']}: KES {d['dividend_amount']:,.2f}")
    else:
        lines.append("  (none recorded yet)")

    lines += ["", "- GCSSHG"]
    return "\n".join(lines)


@app.get("/reports/member-ledger", response_class=HTMLResponse)
def member_ledger_report(request: Request, member_id: int, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            member, entries = build_member_ledger(cur, member_id)
            if not member:
                return HTMLResponse("<p style='font-family:sans-serif;padding:2rem'>Member not found.</p>")
    finally:
        conn.close()
    return templates.TemplateResponse(request, "member_ledger.html", {
        "member": member, "entries": entries, "role": session_data["role"],
    })


@app.post("/reports/member-ledger/email")
def email_member_ledger(member_id: int = Form(...), recipient_email: str = Form(...),
                         session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            member, entries = build_member_ledger(cur, member_id)
    finally:
        conn.close()
    if not member:
        return RedirectResponse(url="/reports?error=Member+not+found", status_code=303)

    body = render_ledger_as_text(member, entries)
    ok, err = send_email(recipient_email, f"GCSSHG ledger - {member['full_name']}", body)
    if ok:
        return RedirectResponse(url=f"/reports/member-ledger?member_id={member_id}&error=Emailed+to+{quote_plus(recipient_email)}", status_code=303)
    return RedirectResponse(url=f"/reports/member-ledger?member_id={member_id}&error=Failed+to+send:+{quote_plus(err or 'unknown error')}", status_code=303)


@app.get("/reports/contributions", response_class=HTMLResponse)
def contributions_report(request: Request, month: Optional[str] = None, year: Optional[int] = None,
                          session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if month:
                month_date = f"{month}-01"
                cur.execute(
                    """SELECT m.full_name, COALESCE(c.amount, 0) as amount
                       FROM members m LEFT JOIN contributions c
                         ON c.member_id = m.id AND c.contribution_month = %s
                       WHERE m.status = 'active' ORDER BY m.full_name""",
                    (month_date,),
                )
                rows = cur.fetchall()
                period_label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
            elif year:
                cur.execute(
                    """SELECT m.full_name, COALESCE(SUM(c.amount), 0) as amount
                       FROM members m LEFT JOIN contributions c
                         ON c.member_id = m.id AND EXTRACT(YEAR FROM c.contribution_month) = %s
                       WHERE m.status = 'active' GROUP BY m.full_name ORDER BY m.full_name""",
                    (year,),
                )
                rows = cur.fetchall()
                period_label = str(year)
            else:
                return RedirectResponse(url="/reports?error=Pick+a+month+or+year", status_code=303)
            total = sum((r["amount"] for r in rows), Decimal("0"))
    finally:
        conn.close()
    return templates.TemplateResponse(request, "report_contributions.html", {
        "rows": rows, "total": total, "period_label": period_label, "role": session_data["role"],
        "month": month, "year": year,
    })


@app.post("/reports/contributions/email")
def email_contributions_report(recipient_email: str = Form(...), month: Optional[str] = Form(None),
                                year: Optional[str] = Form(None), session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if month:
                month_date = f"{month}-01"
                cur.execute(
                    """SELECT m.full_name, COALESCE(c.amount, 0) as amount
                       FROM members m LEFT JOIN contributions c
                         ON c.member_id = m.id AND c.contribution_month = %s
                       WHERE m.status = 'active' ORDER BY m.full_name""",
                    (month_date,),
                )
                rows = cur.fetchall()
                period_label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
            elif year:
                cur.execute(
                    """SELECT m.full_name, COALESCE(SUM(c.amount), 0) as amount
                       FROM members m LEFT JOIN contributions c
                         ON c.member_id = m.id AND EXTRACT(YEAR FROM c.contribution_month) = %s
                       WHERE m.status = 'active' GROUP BY m.full_name ORDER BY m.full_name""",
                    (year,),
                )
                rows = cur.fetchall()
                period_label = str(year)
            else:
                return RedirectResponse(url="/reports?error=Pick+a+month+or+year", status_code=303)
            total = sum((r["amount"] for r in rows), Decimal("0"))
    finally:
        conn.close()

    lines = [f"GCSSHG - Contributions - {period_label}", ""]
    for r in rows:
        lines.append(f"  {r['full_name']:<30} KES {r['amount']:>12,.2f}")
    lines += ["", f"TOTAL: KES {total:,.2f}", "", "- GCSSHG"]
    body = "\n".join(lines)

    ok, err = send_email(recipient_email, f"GCSSHG contributions - {period_label}", body)
    qs = f"month={month}" if month else f"year={year}"
    if ok:
        return RedirectResponse(url=f"/reports/contributions?{qs}&notice={quote_plus('Emailed to ' + recipient_email)}", status_code=303)
    return RedirectResponse(url=f"/reports/contributions?{qs}&notice={quote_plus('Failed to send: ' + (err or 'unknown error'))}", status_code=303)


@app.get("/reports/loans", response_class=HTMLResponse)
def loans_report(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            repaid_map = get_repaid_map(cur)
            cur.execute(
                """SELECT l.*, m.full_name FROM loans l JOIN members m ON m.id = l.member_id
                   ORDER BY (l.status = 'active') DESC, l.issue_date DESC"""
            )
            loans = cur.fetchall()
            loan_rows = [build_loan_detail(cur, loan, date.today(), settings["penalty_amount"], repaid_map) for loan in loans]
            total_principal = sum((l["principal"] for l in loan_rows), Decimal("0"))
            total_paid = sum((l["amount_repaid"] for l in loan_rows), Decimal("0"))
            total_interest = sum(
                (l["current_balance"] - l["principal"] + l["amount_repaid"] for l in loan_rows), Decimal("0")
            )
            total_outstanding = sum((l["current_balance"] for l in loan_rows if l["status"] == "active"), Decimal("0"))
    finally:
        conn.close()
    return templates.TemplateResponse(request, "report_loans.html", {
        "loans": loan_rows, "total_principal": total_principal, "total_paid": total_paid,
        "total_interest": total_interest,
        "total_outstanding": total_outstanding, "role": session_data["role"],
    })


@app.post("/reports/loans/email")
def email_loans_report(recipient_email: str = Form(...), session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            repaid_map = get_repaid_map(cur)
            cur.execute(
                """SELECT l.*, m.full_name FROM loans l JOIN members m ON m.id = l.member_id
                   ORDER BY (l.status = 'active') DESC, l.issue_date DESC"""
            )
            loans = cur.fetchall()
            loan_rows = [build_loan_detail(cur, loan, date.today(), settings["penalty_amount"], repaid_map) for loan in loans]
            total_principal = sum((l["principal"] for l in loan_rows), Decimal("0"))
            total_interest = sum(
                (l["current_balance"] - l["principal"] + l["amount_repaid"] for l in loan_rows), Decimal("0")
            )
            total_outstanding = sum((l["current_balance"] for l in loan_rows if l["status"] == "active"), Decimal("0"))
    finally:
        conn.close()

    lines = ["GCSSHG - All loans & interest", ""]
    for l in loan_rows:
        lines.append(
            f"  {l['full_name']:<25} Issued {l['issue_date']} - Principal KES {l['principal']:>10,.0f} - "
            f"Paid KES {l['amount_repaid']:>10,.2f} - Balance KES {l['current_balance']:>10,.2f} - {l['status']}"
        )
    lines += [
        "", f"Total principal issued: KES {total_principal:,.2f}",
        f"Total paid: KES {sum((l['amount_repaid'] for l in loan_rows), Decimal('0')):,.2f}",
        f"Total interest charged: KES {total_interest:,.2f}",
        f"Currently outstanding: KES {total_outstanding:,.2f}",
        "", "- GCSSHG",
    ]
    body = "\n".join(lines)

    ok, err = send_email(recipient_email, "GCSSHG all loans & interest report", body)
    if ok:
        return RedirectResponse(url=f"/reports/loans?notice={quote_plus('Emailed to ' + recipient_email)}", status_code=303)
    return RedirectResponse(url=f"/reports/loans?notice={quote_plus('Failed to send: ' + (err or 'unknown error'))}", status_code=303)


@app.get("/black-records", response_class=HTMLResponse)
def black_records_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            repaid_map = get_repaid_map(cur)
            cur.execute(
                """SELECT l.*, m.full_name, u.full_name as defaulted_by_name
                   FROM loans l JOIN members m ON m.id = l.member_id
                   LEFT JOIN users u ON u.id = l.defaulted_by
                   WHERE l.status = 'defaulted'
                   ORDER BY l.defaulted_at DESC"""
            )
            defaults = cur.fetchall()
            default_rows = [build_loan_detail(cur, loan, date.today(), settings["penalty_amount"], repaid_map) for loan in defaults]
    finally:
        conn.close()
    return templates.TemplateResponse(request, "black_records.html", {
        "defaults": default_rows, "role": session_data["role"],
    })


@app.post("/loans/{loan_id}/mark-defaulted")
def mark_loan_defaulted(loan_id: int, default_reason: str = Form(""),
                         session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/loans?error=Only+the+chairperson+can+mark+a+loan+as+defaulted", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE loans SET status = 'defaulted', defaulted_at = now(), defaulted_by = %s, default_reason = %s WHERE id = %s",
                (session_data["user_id"], default_reason or None, loan_id),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/black-records?error=Loan+marked+as+defaulted", status_code=303)


@app.post("/loans/{loan_id}/restore-active")
def restore_loan_active(loan_id: int, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/black-records?error=Only+the+chairperson+can+restore+a+loan", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE loans SET status = 'active', defaulted_at = NULL, defaulted_by = NULL, default_reason = NULL WHERE id = %s",
                (loan_id,),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/black-records?error=Loan+restored+to+active", status_code=303)


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            issues = []

            # 0. Funds check - money in the bank plus unrecovered loan
            # principal should be at least what members have contributed. A
            # surplus is healthy (retained, undistributed interest profit).
            # A deficit means money that should exist doesn't.
            reconciliation = compute_funds_reconciliation(cur)
            if reconciliation["surplus"] < Decimal("-1000"):
                issues.append({
                    "severity": "error",
                    "category": "Funds shortfall - assets below contributions",
                    "detail": f"Bank balance ({reconciliation['bank_balance']:,.2f}) plus unpaid loan principal "
                              f"({reconciliation['unpaid_principal']:,.2f}) totals "
                              f"{reconciliation['money_accounted_for']:,.2f}, which is "
                              f"{abs(reconciliation['surplus']):,.2f} SHORT of total contributions "
                              f"({reconciliation['total_contributions']:,.2f}). Check the bank balance is current, "
                              f"and that no contribution or loan was entered outside the system.",
                })

            # 1. Possible duplicate contributions: same member, same month, same
            # amount, entered more than once (top-ups of a DIFFERENT amount in
            # the same month are normal and not flagged).
            cur.execute(
                """SELECT c.member_id, m.full_name, c.contribution_month, c.amount, COUNT(*) as cnt
                   FROM contributions c JOIN members m ON m.id = c.member_id
                   GROUP BY c.member_id, m.full_name, c.contribution_month, c.amount
                   HAVING COUNT(*) > 1
                   ORDER BY c.contribution_month DESC"""
            )
            dup_contributions = cur.fetchall()
            for d in dup_contributions:
                issues.append({
                    "severity": "warning",
                    "category": "Possible duplicate contribution",
                    "detail": f"{d['full_name']}: {d['amount']} entered {d['cnt']} times for "
                              f"{d['contribution_month'].strftime('%b %Y')}",
                    "member_id": d["member_id"],
                })

            # 2 & 3. Loan status vs recomputed balance mismatches
            cur.execute("SELECT l.*, m.full_name FROM loans l JOIN members m ON m.id = l.member_id")
            all_loans = cur.fetchall()
            repaid_map = get_repaid_map(cur)
            for loan in all_loans:
                detail = build_loan_detail(cur, loan, date.today(), settings["penalty_amount"], repaid_map)
                balance = detail["current_balance"]
                if balance < Decimal("-0.01"):
                    issues.append({
                        "severity": "error",
                        "category": "Loan overpaid - excess should be reallocated",
                        "detail": f"{loan['full_name']}: loan #{loan['id']} is overpaid by {abs(balance):,.2f}",
                        "loan_id": loan["id"], "reallocate": True,
                    })
                elif loan["status"] == "cleared" and balance > Decimal("0.01"):
                    issues.append({
                        "severity": "error",
                        "category": "Cleared loan with outstanding balance",
                        "detail": f"{loan['full_name']}: loan #{loan['id']} is marked cleared but shows a "
                                  f"balance of {balance:,.2f}",
                        "loan_id": loan["id"],
                    })
                elif loan["status"] == "active" and balance <= 0:
                    issues.append({
                        "severity": "warning",
                        "category": "Active loan with zero balance",
                        "detail": f"{loan['full_name']}: loan #{loan['id']} is still marked active but its "
                                  f"balance is {balance:,.2f} - likely should be cleared",
                        "loan_id": loan["id"],
                    })
                if loan["status"] in ("active", "cleared") and not loan["due_date"]:
                    issues.append({
                        "severity": "warning",
                        "category": "Loan missing a due date",
                        "detail": f"{loan['full_name']}: loan #{loan['id']} has no due date on record",
                        "loan_id": loan["id"],
                    })

            # 4. Multiple active members sharing the same phone number
            cur.execute(
                """SELECT phone, array_agg(full_name) as names, array_agg(id) as ids, COUNT(*) as cnt
                   FROM members WHERE status = 'active' AND phone IS NOT NULL AND phone != ''
                   GROUP BY phone HAVING COUNT(*) > 1"""
            )
            dup_phones = cur.fetchall()
            for d in dup_phones:
                issues.append({
                    "severity": "error",
                    "category": "Duplicate phone number across members",
                    "detail": f"Phone {d['phone']} is used by {', '.join(d['names'])} - possible accidental duplicate member",
                })

            # 5. Contributions at or below zero (defensive check - schema should prevent this)
            cur.execute(
                """SELECT c.id, m.full_name, c.contribution_month, c.amount FROM contributions c
                   JOIN members m ON m.id = c.member_id WHERE c.amount <= 0"""
            )
            bad_contributions = cur.fetchall()
            for b in bad_contributions:
                issues.append({
                    "severity": "error",
                    "category": "Non-positive contribution amount",
                    "detail": f"{b['full_name']}: {b['amount']} recorded for {b['contribution_month']}",
                    "member_id": None,
                })

            error_count = sum(1 for i in issues if i["severity"] == "error")
            warning_count = sum(1 for i in issues if i["severity"] == "warning")
    finally:
        conn.close()
    return templates.TemplateResponse(request, "audit.html", {
        "issues": issues, "error_count": error_count, "warning_count": warning_count,
        "role": session_data["role"],
    })


@app.api_route("/admin/send-reminders", methods=["GET", "POST"])
def send_reminders_cron(secret: str = ""):
    """
    Called once a day by a scheduled external trigger (Render Cron Job, or
    a free service like cron-job.org - which is why this accepts GET as
    well as POST, since most free cron-ping tools only send GET), passing
    ?secret=... matching REMINDER_SECRET. Not tied to a login session,
    since an automated caller can't log in - the shared secret protects
    this instead.
    """
    if not REMINDER_SECRET or secret != REMINDER_SECRET:
        raise HTTPException(status_code=403, detail="Invalid or missing secret")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            loan_count, contribution_count = run_reminder_checks(cur, date.today())
            conn.commit()
    finally:
        conn.close()
    return {"loan_reminders_sent": loan_count, "contribution_reminders_sent": contribution_count}


@app.post("/settings/run-reminders-now")
def run_reminders_manual(session_data=Depends(get_session_optional)):
    """Lets the chairperson test the reminder check on demand, without waiting for the cron job."""
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/settings?error=Only+the+chairperson+can+do+this", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            loan_count, contribution_count = run_reminder_checks(cur, date.today())
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/settings?error=Sent+{loan_count}+loan+reminder(s)+and+{contribution_count}+contribution+reminder(s)",
        status_code=303,
    )


@app.get("/loans/{loan_id}/reallocate", response_class=HTMLResponse)
def reallocate_overpayment_page(loan_id: int, request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/audit?error=Only+the+chairperson+can+reallocate+an+overpayment", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            cur.execute(
                "SELECT l.*, m.full_name FROM loans l JOIN members m ON m.id = l.member_id WHERE l.id = %s",
                (loan_id,),
            )
            loan = cur.fetchone()
            if not loan:
                return HTMLResponse("<p style='font-family:sans-serif;padding:2rem'>Loan not found.</p>")
            detail = build_loan_detail(cur, loan, date.today(), settings["penalty_amount"])
            excess = abs(min(detail["current_balance"], Decimal("0")))

            cur.execute(
                "SELECT * FROM loans WHERE member_id = %s AND status = 'active' AND id != %s ORDER BY due_date ASC",
                (loan["member_id"], loan_id),
            )
            other_loans_raw = cur.fetchall()
            other_loans = []
            for ol in other_loans_raw:
                od = build_loan_detail(cur, ol, date.today(), settings["penalty_amount"])
                if od["current_balance"] > 0:
                    other_loans.append(od)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "loan_reallocate.html", {
        "loan": detail, "excess": excess, "other_loans": other_loans, "role": session_data["role"],
    })


@app.post("/loans/{loan_id}/reallocate")
def reallocate_overpayment_confirm(loan_id: int, target_loan_id: int = Form(...),
                                    amount: float = Form(...), session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/audit?error=Only+the+chairperson+can+reallocate+an+overpayment", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            reallocate_overpayment(cur, loan_id, target_loan_id, Decimal(str(amount)), session_data["user_id"])
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/audit?error=Overpayment+reallocated", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# ---------------------------------------------------------------------------
# WEB PAGES (server-rendered, mobile-friendly - this is what people actually click)
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def root(session_data=Depends(get_session_optional)):
    if not session_data:
        return RedirectResponse(url="/login")
    if session_data["role"] == "member":
        return RedirectResponse(url="/statement")
    return RedirectResponse(url="/dashboard")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
def login_submit(phone: str = Form(...), password: str = Form(...)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE phone = %s AND is_active = TRUE", (phone,))
            user = cur.fetchone()
            if not user or not user["password_hash"]:
                return RedirectResponse(
                    url="/login?error=Account+not+set+up+yet+-+use+your+invite+link", status_code=303
                )
            if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
                return RedirectResponse(url="/login?error=Invalid+phone+or+password", status_code=303)
            token = create_session_token(user["id"], user["role"])
            redirect_to = "/statement" if user["role"] == "member" else "/dashboard"
            response = RedirectResponse(url=redirect_to, status_code=303)
            response.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 14)
            return response
    finally:
        conn.close()


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request, sent: Optional[str] = None):
    return templates.TemplateResponse(request, "forgot_password.html", {"sent": sent})


@app.post("/forgot-password")
def forgot_password_submit(identifier: str = Form(...)):
    identifier = identifier.strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE (phone = %s OR email = %s) AND is_active = TRUE",
                (identifier, identifier),
            )
            user = cur.fetchone()
            if user and user["email"]:
                token = signer.dumps({"user_id": user["id"], "purpose": "set_password"})
                link = f"{SITE_URL}/set-password?token={token}"
                body = (
                    f"Hello {user['full_name']},\n\n"
                    f"Use this link to reset your GCSSHG password. It's valid for 48 hours.\n\n"
                    f"{link}\n\n"
                    f"If you didn't request this, you can ignore this email."
                )
                send_email(user["email"], "Reset your GCSSHG password", body)
    finally:
        conn.close()
    # Always show the same message, whether or not an account was found or
    # has an email on file - this avoids revealing which phone/email is
    # registered to someone who doesn't already know.
    return RedirectResponse(url="/forgot-password?sent=1", status_code=303)


@app.get("/set-password", response_class=HTMLResponse)
def set_password_page(request: Request, token: str, error: Optional[str] = None):
    try:
        data = signer.loads(token, max_age=60 * 60 * 48)  # 48-hour link
        if data.get("purpose") != "set_password":
            raise itsdangerous.BadSignature("wrong purpose")
    except itsdangerous.BadSignature:
        return HTMLResponse(
            "<p style='font-family:sans-serif;padding:2rem'>"
            "This setup link is invalid or has expired. Ask your chairperson for a new one."
            "</p>"
        )
    return templates.TemplateResponse(request, "set_password.html", {"token": token, "error": error})


@app.post("/set-password")
def set_password_submit(token: str = Form(...), password: str = Form(...), confirm: str = Form(...)):
    try:
        data = signer.loads(token, max_age=60 * 60 * 48)
        if data.get("purpose") != "set_password":
            raise itsdangerous.BadSignature("wrong purpose")
    except itsdangerous.BadSignature:
        return HTMLResponse(
            "<p style='font-family:sans-serif;padding:2rem'>"
            "This setup link is invalid or has expired. Ask your chairperson for a new one."
            "</p>"
        )
    if password != confirm:
        return RedirectResponse(url=f"/set-password?token={token}&error=Passwords+do+not+match", status_code=303)
    if len(password) < 6:
        return RedirectResponse(
            url=f"/set-password?token={token}&error=Password+must+be+at+least+6+characters", status_code=303
        )

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, data["user_id"]))
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/login?error=Password+set+-+please+sign+in", status_code=303)


@app.get("/logout")
def logout_page():
    response = RedirectResponse(url="/login")
    response.delete_cookie("session")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, session_data=Depends(get_session_optional)):
    if not session_data:
        return RedirectResponse(url="/login")
    if session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/statement")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT full_name FROM users WHERE id = %s", (session_data["user_id"],))
            officer_name = cur.fetchone()["full_name"]

            cur.execute("SELECT id FROM members WHERE user_id = %s", (session_data["user_id"],))
            own = cur.fetchone()
            own_member_id = own["id"] if own else None

            cur.execute("SELECT id, full_name FROM members WHERE status = 'active' ORDER BY full_name")
            members = cur.fetchall()

            cur.execute("SELECT COUNT(*) as count FROM members")
            member_count = cur.fetchone()["count"]

            cur.execute("SELECT COALESCE(SUM(amount),0) as total FROM contributions")
            total_contributions = cur.fetchone()["total"]

            # Financial audit figures - chairperson sees the reconciliation
            # check, chairperson and treasurer both see payable dividends.
            reconciliation = compute_funds_reconciliation(cur)
            payable_dividends = get_payable_dividends(cur)

            cur.execute(
                "SELECT COALESCE(SUM(dividend_amount),0) as total FROM dividends"
            )
            total_dividends_paid = cur.fetchone()["total"]

            cur.execute("SELECT COALESCE(SUM(principal),0) as total FROM loans")
            total_loans_issued = cur.fetchone()["total"]

            # Two angles on interest:
            #
            # 1. Expected interest = what the interest calculator says has accrued
            #    across every loan ever issued, as of today (or as of the date a
            #    loan was cleared, so a settled loan doesn't keep "accruing" after
            #    it was actually paid off), MINUS dividends already paid out -
            #    since dividends are only ever shared from interest, once some has
            #    been distributed it's no longer "expected" as undistributed. Can
            #    go negative if dividends paid exceed what's now expected (e.g.
            #    after a loan later defaults) - that's a genuine signal, not a bug.
            #
            # 2. Actual (paid) interest = the sum of interest_component across every
            #    repayment ever recorded. Each repayment already splits itself
            #    between interest and principal at the moment it's paid (interest
            #    first, then principal) - so summing that column directly is exactly
            #    "cash actually received as interest," correct whether a loan is
            #    fully cleared or only partially repaid. (Earlier this was computed
            #    as repaid-minus-full-original-principal per loan, which went
            #    sharply negative for any partially-repaid loan - fixed here.)
            cur.execute("SELECT * FROM loans")
            all_loans_full = cur.fetchall()
            expected_interest_gross = Decimal("0")
            for loan in all_loans_full:
                terms = terms_from_loan_row(loan)
                override = loan.get("interest_override_periods")
                as_of = loan["cleared_date"] if (loan["status"] == "cleared" and loan["cleared_date"]) else date.today()
                interest_due = loan_interest_due(loan["principal"], loan["issue_date"], as_of, terms, override)
                expected_interest_gross += interest_due

            cur.execute("SELECT COALESCE(SUM(interest_component),0) as total FROM loan_repayments")
            actual_interest_paid = cur.fetchone()["total"]

            cur.execute("SELECT COALESCE(SUM(dividend_amount),0) as total FROM dividends")
            dividends_paid_total = cur.fetchone()["total"]
            expected_interest_total = expected_interest_gross - dividends_paid_total

            today = date.today()
            contribution_growth = compute_contribution_growth(cur, today)
            interest_growth = compute_interest_growth(all_loans_full, today)
            timely_payment_pct = compute_timely_payment_pct(all_loans_full, today)

            settings = get_settings(cur)
            cur.execute("SELECT * FROM loans WHERE status = 'active'")
            active_loans = cur.fetchall()
            repaid_map = get_repaid_map(cur)
            total_loans_outstanding = Decimal("0")
            overdue_count = 0
            for loan in active_loans:
                detail = build_loan_detail(cur, loan, date.today(), settings["penalty_amount"], repaid_map)
                total_loans_outstanding += detail["current_balance"]
                if detail["is_overdue"]:
                    overdue_count += 1
            active_ontime_count = len(active_loans) - overdue_count

            cur.execute("SELECT COUNT(*) as count FROM loans WHERE status = 'cleared'")
            cleared_count = cur.fetchone()["count"]

            cur.execute(
                "SELECT contribution_month, SUM(amount) as total FROM contributions "
                "GROUP BY contribution_month ORDER BY contribution_month"
            )
            monthly_rows = cur.fetchall()
            monthly_labels = [r["contribution_month"].strftime("%b %Y") for r in monthly_rows]
            monthly_totals = [float(r["total"]) for r in monthly_rows]
    finally:
        conn.close()
    return templates.TemplateResponse(request, "dashboard.html", {
        "members": members, "role": session_data["role"], "own_member_id": own_member_id,
        "officer_name": officer_name, "greeting": time_greeting(),
        "member_count": member_count,
        "total_contributions": total_contributions,
        "reconciliation": reconciliation, "payable_dividends": payable_dividends,
        "total_loans_outstanding": total_loans_outstanding,
        "total_loans_issued": total_loans_issued,
        "expected_interest_total": expected_interest_total,
        "expected_interest_gross": expected_interest_gross, "dividends_paid_total": dividends_paid_total,
        "contribution_growth": contribution_growth, "interest_growth": interest_growth,
        "timely_payment_pct": timely_payment_pct,
        "actual_interest_paid": actual_interest_paid,
        "total_dividends_paid": total_dividends_paid,
        "monthly_labels": monthly_labels, "monthly_totals": monthly_totals,
        "loan_status_counts": [active_ontime_count, overdue_count, cleared_count],
        "monthly_chart_json": json.dumps({"labels": monthly_labels, "data": monthly_totals}),
        "loan_status_json": json.dumps([active_ontime_count, overdue_count, cleared_count]),
    })


@app.get("/members", response_class=HTMLResponse)
def members_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data:
        return RedirectResponse(url="/login")
    if session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/statement")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT m.*, COALESCE(SUM(c.amount), 0) as total_contributed, u.role as officer_role
                   FROM members m
                   LEFT JOIN contributions c ON c.member_id = m.id
                   LEFT JOIN users u ON u.id = m.user_id
                   WHERE m.status = 'active'
                   GROUP BY m.id, u.role
                   ORDER BY CASE u.role WHEN 'chairperson' THEN 0 WHEN 'treasurer' THEN 1
                            WHEN 'secretary' THEN 2 ELSE 3 END, m.full_name"""
            )
            members = cur.fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "members.html", {
        "members": members, "role": session_data["role"],
    })


@app.get("/members/archived", response_class=HTMLResponse)
def archived_members_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+view+archived+members", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT m.*, COALESCE(SUM(c.amount), 0) as total_contributed
                   FROM members m
                   LEFT JOIN contributions c ON c.member_id = m.id
                   WHERE m.status != 'active'
                   GROUP BY m.id ORDER BY m.full_name"""
            )
            members = cur.fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "members_archived.html", {
        "members": members, "role": session_data["role"],
    })


@app.post("/members/{member_id}/archive")
def archive_member(member_id: int, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/members?error=Only+the+chairperson+can+remove+members", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM members WHERE id = %s", (member_id,))
            row = cur.fetchone()
            cur.execute("UPDATE members SET status = 'exited' WHERE id = %s", (member_id,))
            if row and row["user_id"]:
                cur.execute("UPDATE users SET is_active = FALSE WHERE id = %s", (row["user_id"],))
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/members", status_code=303)


@app.post("/members/{member_id}/restore")
def restore_member(member_id: int, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/members/archived?error=Only+the+chairperson+can+restore+members", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM members WHERE id = %s", (member_id,))
            row = cur.fetchone()
            cur.execute("UPDATE members SET status = 'active' WHERE id = %s", (member_id,))
            if row and row["user_id"]:
                cur.execute("UPDATE users SET is_active = TRUE WHERE id = %s", (row["user_id"],))
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/members/archived", status_code=303)


@app.get("/members/{member_id}/edit", response_class=HTMLResponse)
def edit_member_page(member_id: int, request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/members?error=Only+the+chairperson+can+edit+members", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM members WHERE id = %s", (member_id,))
            member = cur.fetchone()
    finally:
        conn.close()
    if not member:
        return HTMLResponse("<p style='font-family:sans-serif;padding:2rem'>Member not found.</p>")
    return templates.TemplateResponse(request, "member_edit.html", {"member": member, "role": session_data["role"]})


@app.post("/members/{member_id}/edit")
def edit_member_submit(member_id: int, full_name: str = Form(...), phone: str = Form(""),
                        email: str = Form(""), join_date_field: str = Form(...),
                        session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/members?error=Only+the+chairperson+can+edit+members", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE members SET full_name = %s, phone = %s, email = %s, join_date = %s WHERE id = %s "
                "RETURNING user_id",
                (full_name, phone or None, email or None, date.fromisoformat(join_date_field), member_id),
            )
            row = cur.fetchone()
            if row and row["user_id"] and email:
                cur.execute("UPDATE users SET email = %s WHERE id = %s", (email, row["user_id"]))
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/members", status_code=303)


@app.post("/members/{member_id}/set-role")
def set_member_role(member_id: int, new_role: str = Form(...), session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/members?error=Only+the+chairperson+can+assign+leadership+roles", status_code=303)
    if new_role not in ("chairperson", "treasurer", "secretary", "member"):
        return RedirectResponse(url="/members?error=Invalid+role", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, full_name FROM members WHERE id = %s", (member_id,))
            row = cur.fetchone()
            if not row or not row["user_id"]:
                return RedirectResponse(
                    url="/members?error=This+member+needs+a+login+created+first+before+assigning+a+role",
                    status_code=303,
                )
            cur.execute("UPDATE users SET role = %s WHERE id = %s", (new_role, row["user_id"]))
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/members", status_code=303)


@app.get("/loans", response_class=HTMLResponse)
def loans_page(request: Request, q: Optional[str] = None, session_data=Depends(get_session_optional)):
    if not session_data:
        return RedirectResponse(url="/login")
    if session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/statement")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            repaid_map = get_repaid_map(cur)

            cur.execute(
                """SELECT l.*, m.full_name, m.phone FROM loans l
                   JOIN members m ON m.id = l.member_id
                   ORDER BY (l.status = 'active') DESC, l.issue_date DESC"""
            )
            all_loans = cur.fetchall()
            all_loan_rows = [build_loan_detail(cur, loan, date.today(), settings["penalty_amount"], repaid_map) for loan in all_loans]

            # Stats always reflect the WHOLE group, regardless of search
            total_loans_issued = sum((Decimal(l["principal"]) for l in all_loans), Decimal("0"))
            total_repaid = sum((l["amount_repaid"] for l in all_loan_rows), Decimal("0"))
            total_outstanding = sum((l["current_balance"] for l in all_loan_rows if l["status"] == "active"), Decimal("0"))
            overdue_count = sum(1 for l in all_loan_rows if l["is_overdue"])

            # The table itself is filtered by search - done in Python against
            # what's already fetched, rather than a second DB round-trip
            if q and q.strip():
                term = q.strip().lower()
                loan_rows = [
                    l for l in all_loan_rows
                    if term in (l["full_name"] or "").lower() or term in (l["phone"] or "").lower()
                ]
            else:
                loan_rows = all_loan_rows
    finally:
        conn.close()
    return templates.TemplateResponse(request, "loans.html", {
        "loans": loan_rows, "role": session_data["role"], "search_query": q or "",
        "total_loans_issued": total_loans_issued, "total_repaid": total_repaid,
        "total_outstanding": total_outstanding, "overdue_count": overdue_count,
    })


@app.get("/loans/issue", response_class=HTMLResponse)
def issue_loan_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer"):
        return RedirectResponse(url="/loans?error=Only+the+chairperson+or+treasurer+can+issue+loans", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            cur.execute("SELECT id, full_name FROM members WHERE status = 'active' ORDER BY full_name")
            members = cur.fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "loan_issue.html", {
        "members": members, "role": session_data["role"], "high_loan_ceiling": settings["high_loan_ceiling"],
    })


@app.get("/loans/{loan_id}/repay", response_class=HTMLResponse)
def repay_loan_page(loan_id: int, request: Request, q: Optional[str] = None,
                     session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer"):
        return RedirectResponse(url="/loans?error=Only+the+chairperson+or+treasurer+can+record+repayments", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            cur.execute(
                "SELECT l.*, m.full_name FROM loans l JOIN members m ON m.id = l.member_id WHERE l.id = %s",
                (loan_id,),
            )
            loan = cur.fetchone()
            if not loan:
                return HTMLResponse("<p style='font-family:sans-serif;padding:2rem'>Loan not found.</p>")
            detail = build_loan_detail(cur, loan, date.today(), settings["penalty_amount"])
    finally:
        conn.close()
    return templates.TemplateResponse(request, "loan_repay.html", {
        "loan": detail, "role": session_data["role"], "search_query": q or "",
    })


@app.get("/loans/{loan_id}/statement", response_class=HTMLResponse)
def loan_statement_page(loan_id: int, request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            cur.execute(
                "SELECT l.*, m.full_name FROM loans l JOIN members m ON m.id = l.member_id WHERE l.id = %s",
                (loan_id,),
            )
            loan = cur.fetchone()
            if not loan:
                return HTMLResponse("<p style='font-family:sans-serif;padding:2rem'>Loan not found.</p>")
            detail = build_loan_detail(cur, loan, date.today(), settings["penalty_amount"])

            cur.execute(
                "SELECT * FROM loan_repayments WHERE loan_id = %s ORDER BY payment_date", (loan_id,)
            )
            repayments = cur.fetchall()

            schedule = None
            if loan["interest_tier"] == "mid":
                terms = terms_from_loan_row(loan)
                schedule = mid_tier_installment_schedule(loan["principal"], loan["issue_date"], terms)

            cur.execute(
                """SELECT lg.amount_guaranteed, m.full_name FROM loan_guarantors lg
                   JOIN members m ON m.id = lg.guarantor_member_id
                   WHERE lg.loan_id = %s ORDER BY lg.amount_guaranteed DESC""",
                (loan_id,),
            )
            guarantors = cur.fetchall()
            guaranteed_total = sum((g["amount_guaranteed"] for g in guarantors), Decimal("0"))
            self_guaranteed = max(loan["principal"] - guaranteed_total, Decimal("0"))
    finally:
        conn.close()
    return templates.TemplateResponse(request, "loan_statement.html", {
        "loan": detail, "repayments": repayments, "schedule": schedule, "role": session_data["role"],
        "guarantors": guarantors, "self_guaranteed": self_guaranteed,
    })


@app.get("/loans/{loan_id}/agreement", response_class=HTMLResponse)
def loan_agreement_page(loan_id: int, request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT l.*, m.full_name as borrower_name, u.full_name as approved_by_name
                   FROM loans l JOIN members m ON m.id = l.member_id
                   LEFT JOIN users u ON u.id = l.approved_by
                   WHERE l.id = %s""",
                (loan_id,),
            )
            loan = cur.fetchone()
            if not loan:
                return HTMLResponse("<p style='font-family:sans-serif;padding:2rem'>Loan not found.</p>")

            cur.execute(
                """SELECT lg.amount_guaranteed, m.full_name FROM loan_guarantors lg
                   JOIN members m ON m.id = lg.guarantor_member_id
                   WHERE lg.loan_id = %s ORDER BY lg.amount_guaranteed DESC""",
                (loan_id,),
            )
            guarantors = cur.fetchall()
            guaranteed_total = sum((g["amount_guaranteed"] for g in guarantors), Decimal("0"))
            self_guaranteed = max(loan["principal"] - guaranteed_total, Decimal("0"))

            terms = terms_from_loan_row(loan)
            expected_interest = loan_interest_due(
                loan["principal"], loan["issue_date"], loan["due_date"], terms, loan.get("interest_override_periods")
            )
            total_owed = loan["principal"] + expected_interest
    finally:
        conn.close()
    return templates.TemplateResponse(request, "loan_agreement.html", {
        "loan": loan, "guarantors": guarantors, "self_guaranteed": self_guaranteed,
        "expected_interest": expected_interest, "total_owed": total_owed,
        "role": session_data["role"],
    })


@app.post("/loans/issue/review")
def issue_loan_review(request: Request, member_id: int = Form(...), principal: float = Form(...),
                       issue_date_field: str = Form(...),
                       interest_override_periods: Optional[str] = Form(None),
                       confirm_duplicate: Optional[str] = Form(None),
                       session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer"):
        return RedirectResponse(url="/loans?error=Only+the+chairperson+or+treasurer+can+issue+loans", status_code=303)
    issue_date = date.fromisoformat(issue_date_field)
    principal_dec = Decimal(str(principal))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT full_name FROM members WHERE id = %s", (member_id,))
            borrower = cur.fetchone()
            if not borrower:
                return RedirectResponse(url="/loans/issue?error=Member+not+found", status_code=303)

            existing = get_member_active_loan(cur, member_id)
            if existing:
                return RedirectResponse(
                    url=f"/loans/issue?error={quote_plus(borrower['full_name'] + ' already has an active loan - only one loan per member at a time.')}",
                    status_code=303,
                )

            if not confirm_duplicate:
                dup = find_duplicate_loan(cur, member_id, principal_dec, issue_date)
                if dup:
                    return RedirectResponse(
                        url=f"/loans/issue?error=A+loan+of+this+exact+amount+and+date+already+exists+for+this+member+(loan+%23{dup['id']})+-+check+the+box+below+if+this+is+intentional+and+resubmit"
                            f"&prefill_member={member_id}&prefill_principal={principal}&prefill_date={issue_date_field}",
                        status_code=303,
                    )

            settings = get_settings(cur)
            self_capacity = get_member_total_deposits(cur, member_id)
            self_guarantee_amount = min(principal_dec, self_capacity)
            remaining_needed = max(principal_dec - self_capacity, Decimal("0"))

            terms = classify_loan(
                principal_dec,
                low_ceiling=settings["low_loan_ceiling"],
                low_rate=settings["low_loan_interest_rate"],
                mid_rate=settings["high_loan_interest_rate"],
                low_deadline=settings["low_loan_deadline_months"],
                mid_deadline=settings["high_loan_deadline_months"],
                high_ceiling=settings["high_loan_ceiling"],
            )
            due_date = compute_due_date(issue_date, terms)
            expected_interest = loan_interest_due(principal_dec, issue_date, due_date, terms)
            total_owed = principal_dec + expected_interest

            cur.execute("SELECT id, full_name FROM members WHERE status = 'active' AND id != %s ORDER BY full_name", (member_id,))
            other_members = cur.fetchall()
            eligible_guarantors = []
            for m in other_members:
                capacity = get_guarantor_available_capacity(cur, m["id"], settings["penalty_amount"])
                if capacity > 0:
                    eligible_guarantors.append({"id": m["id"], "full_name": m["full_name"], "capacity": capacity})
    finally:
        conn.close()

    return templates.TemplateResponse(request, "loan_issue_review.html", {
        "role": session_data["role"], "borrower_name": borrower["full_name"], "member_id": member_id,
        "principal": principal_dec, "issue_date_field": issue_date_field,
        "interest_override_periods": interest_override_periods or "",
        "self_capacity": self_capacity, "self_guarantee_amount": self_guarantee_amount,
        "remaining_needed": remaining_needed, "eligible_guarantors": eligible_guarantors,
        "terms": terms, "due_date": due_date, "expected_interest": expected_interest, "total_owed": total_owed,
    })


@app.post("/loans/issue/confirm")
async def issue_loan_confirm(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer"):
        return RedirectResponse(url="/loans?error=Only+the+chairperson+or+treasurer+can+issue+loans", status_code=303)
    form = await request.form()
    member_id = int(form.get("member_id"))
    principal_dec = Decimal(form.get("principal"))
    issue_date = date.fromisoformat(form.get("issue_date_field"))
    interest_override_periods = form.get("interest_override_periods", "")
    override_val = int(interest_override_periods) if interest_override_periods else None

    guarantor_allocations = []
    for key, value in form.multi_items():
        if not key.startswith("guarantor_") or not value.strip():
            continue
        try:
            amount = Decimal(value)
        except Exception:
            continue
        if amount <= 0:
            continue
        guarantor_id = int(key.replace("guarantor_", ""))
        guarantor_allocations.append((guarantor_id, amount))

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Re-validate everything fresh - never trust what the browser sent,
            # since capacity may have changed between review and confirm.
            existing = get_member_active_loan(cur, member_id)
            if existing:
                return RedirectResponse(url="/loans/issue?error=This+member+now+has+an+active+loan+-+cannot+issue+another", status_code=303)

            settings = get_settings(cur)
            self_capacity = get_member_total_deposits(cur, member_id)
            self_guarantee_amount = min(principal_dec, self_capacity)
            total_covered = self_guarantee_amount

            for guarantor_id, amount in guarantor_allocations:
                if guarantor_id == member_id:
                    return RedirectResponse(url="/loans/issue?error=A+borrower+cannot+guarantee+their+own+loan", status_code=303)
                capacity = get_guarantor_available_capacity(cur, guarantor_id, settings["penalty_amount"])
                if amount > capacity:
                    cur.execute("SELECT full_name FROM members WHERE id = %s", (guarantor_id,))
                    name = cur.fetchone()["full_name"]
                    return RedirectResponse(
                        url=f"/loans/issue?error={quote_plus(f'{name} only has KES {capacity:,.0f} available to guarantee, not {amount:,.0f}')}",
                        status_code=303,
                    )
                total_covered += amount

            if total_covered < principal_dec:
                return RedirectResponse(
                    url=f"/loans/issue?error={quote_plus(f'Only KES {total_covered:,.0f} of KES {principal_dec:,.0f} is covered by self-guarantee + guarantors - add more guarantors.')}",
                    status_code=303,
                )

            terms = classify_loan(
                principal_dec,
                low_ceiling=settings["low_loan_ceiling"],
                low_rate=settings["low_loan_interest_rate"],
                mid_rate=settings["high_loan_interest_rate"],
                low_deadline=settings["low_loan_deadline_months"],
                mid_deadline=settings["high_loan_deadline_months"],
                high_ceiling=settings["high_loan_ceiling"],
            )
            due_date = compute_due_date(issue_date, terms)
            cur.execute(
                """INSERT INTO loans (member_id, principal, issue_date, due_date, interest_tier,
                                       interest_rate, period_months, approved_by, interest_override_periods)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (member_id, principal_dec, issue_date, due_date, terms.tier, terms.rate_percent,
                 terms.period_months, session_data["user_id"], override_val),
            )
            new_loan_id = cur.fetchone()["id"]

            for guarantor_id, amount in guarantor_allocations:
                cur.execute(
                    "INSERT INTO loan_guarantors (loan_id, guarantor_member_id, amount_guaranteed) VALUES (%s, %s, %s)",
                    (new_loan_id, guarantor_id, amount),
                )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/loans/{new_loan_id}/agreement", status_code=303)


def apply_loan_repayment(cur, loan, amount_dec, payment_date, recorded_by):
    """
    Records one repayment against a loan, splitting it between interest and
    principal, and marks the loan cleared if the balance reaches zero.
    Shared by both the single-loan and bulk repayment routes so the math
    can never drift apart between them.
    """
    terms = terms_from_loan_row(loan)
    override = loan.get("interest_override_periods")
    loan_id = loan["id"]

    interest_due = loan_interest_due(loan["principal"], loan["issue_date"], payment_date, terms, override)
    cur.execute(
        "SELECT COALESCE(SUM(interest_component),0) as paid_interest FROM loan_repayments WHERE loan_id = %s",
        (loan_id,),
    )
    interest_already_paid = cur.fetchone()["paid_interest"]
    interest_outstanding = max(interest_due - interest_already_paid, Decimal("0"))
    interest_component = min(amount_dec, interest_outstanding)
    principal_component = amount_dec - interest_component

    cur.execute(
        """INSERT INTO loan_repayments (loan_id, payment_date, amount, principal_component,
                                         interest_component, recorded_by)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (loan_id, payment_date, amount_dec, principal_component, interest_component, recorded_by),
    )

    cur.execute(
        "SELECT COALESCE(SUM(amount),0) as total_repaid FROM loan_repayments WHERE loan_id = %s",
        (loan_id,),
    )
    total_repaid = cur.fetchone()["total_repaid"]
    full_balance = loan_balance_due(loan["principal"], total_repaid, loan["issue_date"], payment_date, terms, override)
    if full_balance <= 0:
        cur.execute("UPDATE loans SET status = 'cleared', cleared_date = %s WHERE id = %s",
                    (payment_date, loan_id))
    return full_balance


def find_duplicate_loan(cur, member_id, principal, issue_date, exclude_loan_id=None):
    """
    A 'duplicate' is the same member, same principal amount, same issue
    date - the exact signature of accidentally submitting the same loan
    twice. Returns the existing loan row if found, else None.
    """
    query = "SELECT * FROM loans WHERE member_id = %s AND principal = %s AND issue_date = %s"
    params = [member_id, principal, issue_date]
    if exclude_loan_id:
        query += " AND id != %s"
        params.append(exclude_loan_id)
    cur.execute(query, params)
    return cur.fetchone()


def get_loan_balance(cur, loan, as_of):
    cur.execute(
        "SELECT COALESCE(SUM(amount),0) as repaid FROM loan_repayments WHERE loan_id = %s",
        (loan["id"],),
    )
    repaid = cur.fetchone()["repaid"]
    terms = terms_from_loan_row(loan)
    override = loan.get("interest_override_periods")
    return loan_balance_due(loan["principal"], repaid, loan["issue_date"], as_of, terms, override)


def reallocate_overpayment(cur, from_loan_id, to_loan_id, amount, recorded_by):
    """
    Moves an overpayment from one loan to another, keeping a full audit
    trail: the original overpayment stays visible, paired with a negative
    correction entry showing exactly where the excess went and a matching
    positive entry on the loan it landed on.
    """
    today = date.today()
    cur.execute(
        """INSERT INTO loan_repayments (loan_id, payment_date, amount, principal_component, interest_component, recorded_by)
           VALUES (%s, %s, %s, %s, 0, %s)""",
        (from_loan_id, today, -amount, -amount, recorded_by),
    )
    cur.execute("SELECT * FROM loans WHERE id = %s", (to_loan_id,))
    to_loan = cur.fetchone()
    apply_loan_repayment(cur, to_loan, amount, today, recorded_by)

    # If the correction brought the source loan's balance back to exactly
    # zero and it had been marked cleared while overpaid, its status is
    # already correct; if it was active and is now settled, mark it cleared.
    cur.execute("SELECT * FROM loans WHERE id = %s", (from_loan_id,))
    from_loan = cur.fetchone()
    new_balance = get_loan_balance(cur, from_loan, today)
    if from_loan["status"] == "active" and new_balance <= 0:
        cur.execute("UPDATE loans SET status = 'cleared', cleared_date = %s WHERE id = %s", (today, from_loan_id))


def fix_negative_balance_loan(cur, loan_id, recorded_by, penalty_amount):
    """
    If this loan's balance is already negative (overpaid, from before the
    overflow-cascade fix existed), moves the excess onto the same member's
    other active loans - oldest due date first - by trimming back the most
    recent repayment(s) on this loan that caused the overpayment and
    recording that same amount as a fresh repayment on the destination
    loan(s), dated the same as the trimmed repayment. Returns the amount
    actually moved (0 if there was nothing to fix).
    """
    cur.execute("SELECT * FROM loans WHERE id = %s", (loan_id,))
    loan = cur.fetchone()
    if not loan:
        return Decimal("0")

    balance = get_loan_balance(cur, loan, date.today())
    if balance >= 0:
        return Decimal("0")
    excess = -balance

    # Trim back the most recent repayment(s) on this loan until the excess
    # is fully accounted for, remembering the date(s) it came from.
    cur.execute(
        "SELECT * FROM loan_repayments WHERE loan_id = %s ORDER BY payment_date DESC, id DESC",
        (loan_id,),
    )
    repayments = cur.fetchall()
    remaining_to_trim = excess
    trimmed_batches = []  # list of (amount, payment_date)
    for r in repayments:
        if remaining_to_trim <= 0:
            break
        trim_amount = min(remaining_to_trim, r["amount"])
        trimmed_batches.append((trim_amount, r["payment_date"]))
        if trim_amount == r["amount"]:
            cur.execute("DELETE FROM loan_repayments WHERE id = %s", (r["id"],))
        else:
            ratio = (r["amount"] - trim_amount) / r["amount"]
            new_principal = (r["principal_component"] * ratio).quantize(Decimal("0.01"))
            new_interest = (r["amount"] - trim_amount) - new_principal
            cur.execute(
                "UPDATE loan_repayments SET amount = %s, principal_component = %s, interest_component = %s WHERE id = %s",
                (r["amount"] - trim_amount, new_principal, new_interest, r["id"]),
            )
        remaining_to_trim -= trim_amount

    moved_total = excess - remaining_to_trim
    if moved_total <= 0:
        return Decimal("0")

    # Reflect the trim on the source loan (auto-restores 'active' status if
    # it had been incorrectly marked cleared while overpaid).
    new_balance = get_loan_balance(cur, loan, date.today())
    if new_balance > 0 and loan["status"] == "cleared":
        cur.execute("UPDATE loans SET status = 'active', cleared_date = NULL WHERE id = %s", (loan_id,))

    # Apply the moved amount to the member's other active loans, oldest due first.
    cur.execute(
        "SELECT * FROM loans WHERE member_id = %s AND status = 'active' AND id != %s ORDER BY due_date ASC",
        (loan["member_id"], loan_id),
    )
    other_loans = cur.fetchall()
    remaining = moved_total
    move_date = trimmed_batches[0][1] if trimmed_batches else date.today()
    for other in other_loans:
        if remaining <= 0:
            break
        other_balance = get_loan_balance(cur, other, move_date)
        if other_balance <= 0:
            continue
        pay_amount = min(remaining, other_balance)
        apply_loan_repayment(cur, other, pay_amount, move_date, recorded_by)
        remaining -= pay_amount

    return moved_total - remaining


def apply_repayment_with_overflow(cur, loan_id, amount_dec, payment_date, recorded_by, exclude_loan_ids=None):
    """
    Applies a payment to the chosen loan first (up to its balance), then
    rolls any excess onto the same member's other active loans, oldest due
    date first - so an overpayment never just sits as a negative balance
    on one loan. Returns (list of (loan_id, amount_applied) actually
    recorded, leftover) - leftover is > 0 only if the member has no more
    active loans left to absorb the excess.

    exclude_loan_ids: loan IDs to skip when cascading (used by bulk entry,
    where those loans already have their own separately-entered amount in
    the same batch - so this won't silently double up or override an
    explicit per-row allocation the officer already made).
    """
    exclude_loan_ids = exclude_loan_ids or set()
    cur.execute("SELECT * FROM loans WHERE id = %s", (loan_id,))
    target_loan = cur.fetchone()
    if not target_loan:
        return [], amount_dec

    cur.execute(
        "SELECT * FROM loans WHERE member_id = %s AND status = 'active' ORDER BY due_date ASC",
        (target_loan["member_id"],),
    )
    other_active = [l for l in cur.fetchall() if l["id"] != loan_id and l["id"] not in exclude_loan_ids]
    ordered_loans = [target_loan] + other_active

    remaining = amount_dec
    applied = []
    for loan in ordered_loans:
        if remaining <= 0:
            break
        balance = get_loan_balance(cur, loan, payment_date)
        if balance <= 0:
            continue
        pay_amount = min(remaining, balance)
        apply_loan_repayment(cur, loan, pay_amount, payment_date, recorded_by)
        applied.append((loan["id"], pay_amount))
        remaining -= pay_amount

    return applied, remaining


@app.post("/dashboard/repay-loan")
def repay_loan_form(loan_id: int = Form(...), amount: float = Form(...),
                     payment_date_field: str = Form(...), search_query: str = Form(""),
                     session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer"):
        return RedirectResponse(url="/loans?error=Only+the+chairperson+or+treasurer+can+record+repayments", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM loans WHERE id = %s", (loan_id,))
            loan = cur.fetchone()
            if not loan:
                return RedirectResponse(url="/loans?error=Loan+not+found", status_code=303)
            payment_date = date.fromisoformat(payment_date_field)
            amount_dec = Decimal(str(amount))
            applied, leftover = apply_repayment_with_overflow(cur, loan_id, amount_dec, payment_date, session_data["user_id"])
            conn.commit()
    finally:
        conn.close()

    if len(applied) > 1:
        message = f"Repayment+recorded+-+spread+across+{len(applied)}+loans"
    else:
        message = "Repayment+recorded"
    if leftover > 0:
        message += f"+-+KES+{leftover:,.0f}+could+not+be+applied+(no+more+active+loans)"

    if search_query:
        return RedirectResponse(url=f"/loans?success={message}&q={quote_plus(search_query)}", status_code=303)
    return RedirectResponse(url=f"/loans/{loan_id}/statement?success={message}", status_code=303)


@app.post("/loans/bulk-repay")
async def bulk_repay_loans(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer"):
        return RedirectResponse(url="/loans?error=Only+the+chairperson+or+treasurer+can+record+repayments", status_code=303)
    form = await request.form()
    default_date_field = form.get("payment_date_field", "").strip()
    default_date = date.fromisoformat(default_date_field) if default_date_field else None
    search_query = form.get("search_query", "")

    conn = get_conn()
    saved = 0
    skipped_no_date = 0
    total_leftover = Decimal("0")
    try:
        with conn.cursor() as cur:
            # Collect every loan that has its own explicit amount in this
            # batch first, so cascading from one loan's overpayment never
            # overrides an amount the officer separately entered for another
            # loan in the same submission.
            touched_loan_ids = set()
            for key, value in form.multi_items():
                if key.startswith("amount_") and value.strip():
                    try:
                        if Decimal(value) > 0:
                            touched_loan_ids.add(int(key.replace("amount_", "")))
                    except Exception:
                        pass

            for key, value in form.multi_items():
                if not key.startswith("amount_") or not value.strip():
                    continue
                try:
                    amount_dec = Decimal(value)
                except Exception:
                    continue
                if amount_dec <= 0:
                    continue
                loan_id = int(key.replace("amount_", ""))

                # Each row can carry its own date (date_<loan_id>) - falls back
                # to the shared default date field if the row didn't set one.
                row_date_field = form.get(f"date_{loan_id}", "").strip()
                if row_date_field:
                    payment_date = date.fromisoformat(row_date_field)
                elif default_date:
                    payment_date = default_date
                else:
                    skipped_no_date += 1
                    continue

                # Applied to THIS loan first (up to its balance, as entered),
                # then any excess automatically rolls onto this same member's
                # OTHER active loans NOT otherwise touched in this batch - so
                # an amount typed against one loan is never lost to a negative
                # balance, and never silently overrides a separate amount the
                # officer entered for another one of that member's loans.
                other_touched = touched_loan_ids - {loan_id}
                applied, leftover = apply_repayment_with_overflow(
                    cur, loan_id, amount_dec, payment_date, session_data["user_id"],
                    exclude_loan_ids=other_touched,
                )
                if applied:
                    saved += 1
                total_leftover += leftover
            conn.commit()
    finally:
        conn.close()

    message = f"{saved}+repayment(s)+recorded"
    if total_leftover > 0:
        message += f"+-+KES+{total_leftover:,.0f}+could+not+be+applied+(no+more+active+loans+for+some+members)"
    if skipped_no_date:
        message += f"+-+{skipped_no_date}+skipped+(no+date+given)"
    redirect_url = f"/loans?success={message}"
    if search_query:
        redirect_url += f"&q={quote_plus(search_query)}"
    return RedirectResponse(url=redirect_url, status_code=303)


@app.post("/loans/{loan_id}/delete")
def delete_loan(loan_id: int, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/loans?error=Only+the+chairperson+can+delete+loans", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM loans WHERE id = %s", (loan_id,))
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/loans", status_code=303)


@app.get("/loans/{loan_id}/edit", response_class=HTMLResponse)
def edit_loan_page(loan_id: int, request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/loans?error=Only+the+chairperson+can+edit+loans", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT l.*, m.full_name FROM loans l JOIN members m ON m.id = l.member_id WHERE l.id = %s",
                (loan_id,),
            )
            loan = cur.fetchone()
    finally:
        conn.close()
    if not loan:
        return HTMLResponse("<p style='font-family:sans-serif;padding:2rem'>Loan not found.</p>")
    return templates.TemplateResponse(request, "loan_edit.html", {"loan": loan, "role": session_data["role"]})


@app.post("/loans/{loan_id}/edit")
def edit_loan(loan_id: int, principal: float = Form(...), issue_date_field: str = Form(...),
              interest_override_periods: Optional[str] = Form(None),
              session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/loans?error=Only+the+chairperson+can+edit+loans", status_code=303)
    override_val = int(interest_override_periods) if interest_override_periods else None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            terms = classify_loan(
                Decimal(str(principal)),
                low_ceiling=settings["low_loan_ceiling"],
                low_rate=settings["low_loan_interest_rate"],
                mid_rate=settings["high_loan_interest_rate"],
                low_deadline=settings["low_loan_deadline_months"],
                mid_deadline=settings["high_loan_deadline_months"],
                high_ceiling=settings["high_loan_ceiling"],
            )
            issue_date = date.fromisoformat(issue_date_field)
            due_date = compute_due_date(issue_date, terms)
            cur.execute(
                """UPDATE loans SET principal = %s, issue_date = %s, due_date = %s,
                   interest_tier = %s, interest_rate = %s, period_months = %s,
                   interest_override_periods = %s WHERE id = %s""",
                (principal, issue_date, due_date, terms.tier, terms.rate_percent,
                 terms.period_months, override_val, loan_id),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/loans", status_code=303)


@app.get("/officers", response_class=HTMLResponse)
def officers_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+manage+leadership+roles", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE role IN ('chairperson','treasurer','secretary') "
                "ORDER BY role, full_name"
            )
            officers = cur.fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "officers.html", {
        "officers": officers, "role": session_data["role"], "own_user_id": session_data["user_id"],
    })


@app.post("/officers/{user_id}/change-role")
def change_officer_role(user_id: int, new_role: str = Form(...),
                         session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+manage+leadership+roles", status_code=303)
    if new_role not in ("chairperson", "treasurer", "secretary", "member"):
        return RedirectResponse(url="/officers?error=Invalid+role", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET role = %s WHERE id = %s", (new_role, user_id))
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/officers?error=Role+updated", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+view+settings", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "settings.html", {"settings": settings, "role": session_data["role"]})


@app.post("/settings/update")
def settings_update(
    min_monthly_contribution: float = Form(...),
    low_loan_ceiling: float = Form(...),
    high_loan_ceiling: float = Form(...),
    low_loan_interest_rate: float = Form(...),
    high_loan_interest_rate: float = Form(...),
    low_loan_deadline_months: int = Form(...),
    high_loan_deadline_months: int = Form(...),
    penalty_amount: float = Form(...),
    dividend_admin_percent: float = Form(...),
    dividend_transaction_percent: float = Form(...),
    dividend_admin_label: str = Form("Administration"),
    dividend_transaction_label: str = Form("Transaction costs"),
    session_data=Depends(get_session_optional),
):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+change+settings", status_code=303)
    if dividend_admin_percent + dividend_transaction_percent > 100:
        return RedirectResponse(
            url="/settings?error=Admin+%2B+transaction+percentages+can't+exceed+100%25", status_code=303
        )
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE group_settings SET
                   min_monthly_contribution = %s, low_loan_ceiling = %s, high_loan_ceiling = %s,
                   low_loan_interest_rate = %s, high_loan_interest_rate = %s,
                   low_loan_deadline_months = %s, high_loan_deadline_months = %s,
                   penalty_amount = %s, dividend_admin_percent = %s, dividend_transaction_percent = %s,
                   dividend_admin_label = %s, dividend_transaction_label = %s,
                   updated_at = now()""",
                (min_monthly_contribution, low_loan_ceiling, high_loan_ceiling,
                 low_loan_interest_rate, high_loan_interest_rate,
                 low_loan_deadline_months, high_loan_deadline_months, penalty_amount,
                 dividend_admin_percent, dividend_transaction_percent,
                 dividend_admin_label.strip() or "Administration",
                 dividend_transaction_label.strip() or "Transaction costs"),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/settings?error=Settings+saved", status_code=303)


def generate_penalties(cur, settings, today):
    """
    Self-correcting: for every active overdue loan and every member behind
    on contributions, replaces any existing UNPAID, UNWAIVED penalty for
    that exact reason with one fresh entry reflecting the CURRENT amount
    owed. If a loan is no longer overdue, or a member has caught up, any
    stale unpaid entry for that reason is removed and nothing new is
    charged - penalties always reflect today's reality, not a permanent
    tally of every threshold ever crossed in the past.

    Already PAID or WAIVED penalties are never touched - that's settled
    history, not something this recalculates.

    Each reason gets exactly ONE consolidated entry (e.g. "3 months
    overdue" as one KES 150 line, not three separate KES 50 lines), with
    a plain-language period_label explaining the calculation.

    Safe to run repeatedly - every call is a full self-correction, not an
    additive scan, so the regular check and a manual recalculation behave
    identically.
    """
    penalty_amount = settings["penalty_amount"]
    changed = 0

    # --- Overdue loan penalties: one consolidated entry per loan ---
    # Checks EVERY loan regardless of status - a loan that was overdue and
    # has since been fully repaid (status -> cleared) still needs its old
    # unpaid penalty entry cleaned up here; is_loan_overdue() already
    # correctly returns False once balance <= 0, so nothing new gets
    # charged for a settled loan, but the stale charge still gets removed.
    cur.execute("SELECT * FROM loans WHERE due_date IS NOT NULL")
    loans = cur.fetchall()
    for loan in loans:
        cur.execute(
            "SELECT COALESCE(SUM(amount),0) as repaid FROM loan_repayments WHERE loan_id = %s",
            (loan["id"],),
        )
        repaid = cur.fetchone()["repaid"]
        terms = terms_from_loan_row(loan)
        balance = loan_balance_due(loan["principal"], repaid, loan["issue_date"], today, terms)

        cur.execute(
            "DELETE FROM penalties WHERE reference_id = %s AND penalty_type = 'overdue_loan' "
            "AND paid = FALSE AND waived = FALSE",
            (loan["id"],),
        )
        changed += cur.rowcount

        if is_loan_overdue(loan["due_date"], balance, today):
            months_overdue = loan_overdue_months(loan["due_date"], today)
            amount = (penalty_amount * months_overdue).quantize(Decimal("0.01"))
            period_label = f"Overdue {months_overdue} month(s) as of {today.strftime('%d %b %Y')} ({months_overdue} x {penalty_amount:.0f})"
            cur.execute(
                """INSERT INTO penalties (member_id, penalty_type, reference_id, period_label, amount)
                   VALUES (%s, 'overdue_loan', %s, %s, %s)""",
                (loan["member_id"], loan["id"], period_label, amount),
            )
            changed += 1

    # --- Missed monthly contribution penalties: one consolidated entry per member ---
    # Cumulative check: a member is only penalized if their TOTAL contribution
    # to date falls short of (min_monthly_contribution x months elapsed since
    # joining). Someone who paid 1000 in March and 0 in April is NOT penalized -
    # their running total already covers both months. This avoids fining
    # members who front-load or pay unevenly but keep pace overall.
    cur.execute("SELECT id, join_date FROM members WHERE status = 'active'")
    members = cur.fetchall()
    min_contribution = settings["min_monthly_contribution"]
    for m in members:
        cur.execute(
            "DELETE FROM penalties WHERE member_id = %s AND penalty_type = 'missed_contribution' "
            "AND paid = FALSE AND waived = FALSE",
            (m["id"],),
        )
        changed += cur.rowcount

        standing = compute_contribution_standing(cur, m["id"], m["join_date"], min_contribution, today)
        if standing["is_current"]:
            continue  # caught up overall - no penalty, regardless of which months were light

        shortfall_months = math.ceil(standing["shortfall"] / min_contribution)
        amount = (penalty_amount * shortfall_months).quantize(Decimal("0.01"))
        period_label = (
            f"Behind {shortfall_months} month(s) as of {today.strftime('%d %b %Y')} "
            f"(KES {standing['shortfall']:,.0f} shortfall / {min_contribution:.0f} = {shortfall_months} x {penalty_amount:.0f})"
        )
        cur.execute(
            """INSERT INTO penalties (member_id, penalty_type, reference_id, period_label, amount)
               VALUES (%s, 'missed_contribution', NULL, %s, %s)""",
            (m["id"], period_label, amount),
        )
        changed += 1

    return changed


@app.post("/admin/run-penalty-check")
def run_penalty_check(session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer"):
        return RedirectResponse(url="/penalties?error=Only+the+chairperson+or+treasurer+can+run+this+check", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            changed = generate_penalties(cur, settings, date.today())
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/penalties?error=Penalty+check+complete+-+{changed}+entries+updated+to+match+current+standing", status_code=303)


@app.post("/admin/recalculate-penalties")
def recalculate_penalties(session_data=Depends(get_session_optional)):
    """
    Same self-correcting logic as the regular check - kept as a separate
    action for anyone used to reaching for "recalculate" specifically, but
    functionally identical, since the regular check is now always
    self-correcting too. Never touches PAID or WAIVED penalties.
    """
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/penalties?error=Only+the+chairperson+can+recalculate+penalties", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            changed = generate_penalties(cur, settings, date.today())
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/penalties?error=Recalculated+-+{changed}+entries+updated+to+match+current+standing",
        status_code=303,
    )


@app.post("/admin/wipe-and-recalculate-penalties")
def wipe_and_recalculate_penalties(session_data=Depends(get_session_optional)):
    """
    The full reset: deletes EVERY penalty - including ones already marked
    paid or waived - then rebuilds from scratch based on current standing.
    Unlike the regular recalculate (which preserves paid/waived history),
    this erases that history entirely, so use it only when you specifically
    want a clean slate and don't need a record of past penalty payments.
    """
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/penalties?error=Only+the+chairperson+can+do+this", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM penalties")
            removed = cur.rowcount
            settings = get_settings(cur)
            changed = generate_penalties(cur, settings, date.today())
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/penalties?error=Wiped+all+{removed}+penalty+records+and+rebuilt+{changed}+fresh+from+current+standing",
        status_code=303,
    )


@app.get("/penalties", response_class=HTMLResponse)
def penalties_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT p.*, m.full_name FROM penalties p
                   JOIN members m ON m.id = p.member_id
                   ORDER BY p.paid ASC, p.waived ASC, p.created_at DESC"""
            )
            penalties = cur.fetchall()
            total_outstanding = sum(
                (Decimal(p["amount"]) for p in penalties if not p["waived"] and not p["paid"]), Decimal("0")
            )
            total_paid = sum((Decimal(p["amount"]) for p in penalties if p["paid"]), Decimal("0"))
    finally:
        conn.close()
    return templates.TemplateResponse(request, "penalties.html", {
        "penalties": penalties, "total_outstanding": total_outstanding,
        "total_paid": total_paid, "role": session_data["role"],
    })


@app.post("/penalties/{penalty_id}/mark-paid")
def mark_penalty_paid(penalty_id: int, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer"):
        return RedirectResponse(url="/penalties?error=Only+the+chairperson+or+treasurer+can+record+penalty+payments", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE penalties SET paid = TRUE, paid_at = now(), paid_recorded_by = %s WHERE id = %s",
                (session_data["user_id"], penalty_id),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/penalties", status_code=303)


@app.post("/penalties/{penalty_id}/waive")
def waive_penalty(penalty_id: int, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/penalties?error=Only+the+chairperson+can+waive+penalties", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE penalties SET waived = TRUE, waived_by = %s WHERE id = %s",
                (session_data["user_id"], penalty_id),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/penalties", status_code=303)


def _render_invite_link(member_name: str, link: str) -> HTMLResponse:
    return HTMLResponse(f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link href="https://fonts.googleapis.com/css2?family=Fraunces:wght@600&family=IBM+Plex+Sans:wght@400;600&display=swap" rel="stylesheet">
    <style>
      body {{ font-family:'IBM Plex Sans',sans-serif; background:#F6F5F1; color:#17231C; padding:2rem; max-width:520px; margin:0 auto; }}
      h1 {{ font-family:'Fraunces',serif; font-size:1.2rem; }}
      .card {{ background:#fff; border:1px solid #E1E4DE; border-left:3px dashed #E1E4DE; border-radius:8px; padding:1.2rem; }}
      .link-box {{ background:#F3E4C6; padding:0.8rem; border-radius:6px; word-break:break-all; font-family:monospace; font-size:0.85rem; margin:0.8rem 0; }}
      a.btn {{ display:inline-block; background:#1F4D3D; color:white; padding:0.6rem 1rem; border-radius:6px; text-decoration:none; margin-top:0.8rem; }}
    </style></head>
    <body>
      <div class="card">
        <h1>Setup link for {member_name}</h1>
        <p>Send this link to them over WhatsApp or SMS. It's valid for 48 hours and lets them set their own password.</p>
        <div class="link-box">{link}</div>
        <a class="btn" href="/members">&larr; Back to members</a>
      </div>
    </body></html>
    """)


def render_activation_email_body(member_name: str, link: str) -> str:
    first_name = member_name.split(" ")[0]
    return (
        f"Hello {first_name},\n\n"
        f"Welcome to the GCSSHG online system! You can now view your contributions, loans, "
        f"penalties, and dividends anytime from your phone.\n\n"
        f"STEP 1 - SET YOUR PASSWORD\n"
        f"Open this link to create your password (valid for 48 hours):\n"
        f"{link}\n\n"
        f"Once set, sign in anytime at {SITE_URL}/login using your phone number and that password.\n\n"
        f"STEP 2 - INSTALL THE APP ON YOUR PHONE (optional, but recommended)\n\n"
        f"On Android (Chrome):\n"
        f"  1. Open {SITE_URL} in Chrome\n"
        f"  2. Tap the menu (three dots, top right) and choose \"Add to Home screen\" or \"Install app\"\n"
        f"  3. Confirm - the GCSSHG icon will appear on your home screen\n\n"
        f"On iPhone (Safari - this only works in Safari, not Chrome):\n"
        f"  1. Open {SITE_URL} in Safari\n"
        f"  2. Tap the Share icon (square with an arrow up) at the bottom of the screen\n"
        f"  3. Scroll down and tap \"Add to Home Screen\"\n\n"
        f"Once installed, tap the GCSSHG icon anytime to check your statement - no need to open a browser.\n\n"
        f"If you have any trouble, contact your chairperson, treasurer, or secretary.\n\n"
        f"- GCSSHG"
    )


@app.get("/members/activation-emails", response_class=HTMLResponse)
def activation_emails_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/members?error=Only+the+chairperson+can+do+this", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, full_name, phone, email FROM members
                   WHERE status = 'active' AND user_id IS NULL ORDER BY full_name"""
            )
            candidates = cur.fetchall()
    finally:
        conn.close()
    eligible = [m for m in candidates if m["phone"] and m["email"]]
    ineligible = [m for m in candidates if not (m["phone"] and m["email"])]
    return templates.TemplateResponse(request, "activation_emails.html", {
        "eligible": eligible, "ineligible": ineligible, "role": session_data["role"],
    })


@app.post("/members/send-activation-emails")
async def send_activation_emails(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/members?error=Only+the+chairperson+can+do+this", status_code=303)
    form = await request.form()
    selected_ids = [int(v) for k, v in form.multi_items() if k == "member_ids"]
    if not selected_ids:
        return RedirectResponse(url="/members/activation-emails?error=Pick+at+least+one+member", status_code=303)

    conn = get_conn()
    sent, skipped = 0, []
    try:
        with conn.cursor() as cur:
            for mid in selected_ids:
                cur.execute("SELECT * FROM members WHERE id = %s", (mid,))
                member = cur.fetchone()
                if not member:
                    continue
                if member["user_id"]:
                    skipped.append(f"{member['full_name']}: already has a login")
                    continue
                if not member["phone"] or not member["email"]:
                    skipped.append(f"{member['full_name']}: missing phone or email")
                    continue
                cur.execute("SELECT id FROM users WHERE phone = %s", (member["phone"],))
                if cur.fetchone():
                    skipped.append(f"{member['full_name']}: phone already registered to another account")
                    continue

                cur.execute(
                    "INSERT INTO users (phone, full_name, password_hash, role, email) "
                    "VALUES (%s, %s, NULL, 'member', %s) RETURNING id",
                    (member["phone"], member["full_name"], member["email"]),
                )
                user_id = cur.fetchone()["id"]
                cur.execute("UPDATE members SET user_id = %s WHERE id = %s", (user_id, mid))

                token = signer.dumps({"user_id": user_id, "purpose": "set_password"})
                link = f"{SITE_URL}/set-password?token={token}"
                body = render_activation_email_body(member["full_name"], link)
                ok, err = send_email(member["email"], "Welcome to GCSSHG - activate your account", body)
                if ok:
                    sent += 1
                else:
                    skipped.append(f"{member['full_name']}: email failed ({err})")
            conn.commit()
    finally:
        conn.close()

    message = f"Sent to {sent} member(s)"
    if skipped:
        message += f" - {len(skipped)} skipped: {'; '.join(skipped[:5])}"
        if len(skipped) > 5:
            message += f" (+{len(skipped) - 5} more)"
    return RedirectResponse(url=f"/members/activation-emails?{urlencode({'error': message})}", status_code=303)



@app.post("/members/{member_id}/create-login")
def create_member_login(member_id: int, phone: str = Form(...), session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/members?error=Only+the+chairperson+can+create+member+logins", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM members WHERE id = %s", (member_id,))
            member = cur.fetchone()
            if not member:
                return RedirectResponse(url="/members?error=Member+not+found", status_code=303)
            if member["user_id"]:
                return RedirectResponse(url="/members?error=This+member+already+has+a+login", status_code=303)
            cur.execute("SELECT id FROM users WHERE phone = %s", (phone,))
            if cur.fetchone():
                return RedirectResponse(
                    url="/members?error=That+phone+number+is+already+registered+to+another+account",
                    status_code=303,
                )
            cur.execute(
                "INSERT INTO users (phone, full_name, password_hash, role) "
                "VALUES (%s, %s, NULL, 'member') RETURNING id",
                (phone, member["full_name"]),
            )
            user_id = cur.fetchone()["id"]
            cur.execute("UPDATE members SET user_id = %s, phone = %s WHERE id = %s", (user_id, phone, member_id))
            conn.commit()
    finally:
        conn.close()

    token = signer.dumps({"user_id": user_id, "purpose": "set_password"})
    link = f"{SITE_URL}/set-password?token={token}"
    return _render_invite_link(member["full_name"], link)


@app.post("/members/{member_id}/link-existing")
def link_existing_account(member_id: int, existing_phone: str = Form(...),
                           session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/members?error=Only+the+chairperson+can+link+accounts", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM members WHERE id = %s", (member_id,))
            member = cur.fetchone()
            if not member:
                return RedirectResponse(url="/members?error=Member+not+found", status_code=303)
            if member["user_id"]:
                return RedirectResponse(url="/members?error=This+member+already+has+a+login", status_code=303)
            cur.execute("SELECT id, full_name FROM users WHERE phone = %s", (existing_phone,))
            existing_user = cur.fetchone()
            if not existing_user:
                return RedirectResponse(
                    url=f"/members?error=No+account+found+with+phone+{existing_phone}", status_code=303
                )
            cur.execute("SELECT id FROM members WHERE user_id = %s", (existing_user["id"],))
            if cur.fetchone():
                return RedirectResponse(
                    url="/members?error=That+account+is+already+linked+to+a+different+member", status_code=303
                )
            cur.execute(
                "UPDATE members SET user_id = %s, phone = %s WHERE id = %s",
                (existing_user["id"], existing_phone, member_id),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/members", status_code=303)


@app.post("/members/{member_id}/resend-invite")
def resend_member_invite(member_id: int, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/members?error=Only+the+chairperson+can+resend+invites", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT m.*, u.id as uid FROM members m JOIN users u ON u.id = m.user_id WHERE m.id = %s",
                        (member_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return RedirectResponse(url="/members?error=This+member+has+no+login+yet", status_code=303)

    token = signer.dumps({"user_id": row["uid"], "purpose": "set_password"})
    link = f"{SITE_URL}/set-password?token={token}"
    return _render_invite_link(row["full_name"], link)


@app.post("/dashboard/add-member")
def add_member_form(full_name: str = Form(...), phone: str = Form(""),
                     session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/members?error=Only+the+chairperson+can+add+members", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO members (full_name, phone) VALUES (%s, %s)",
                (full_name, phone or None),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/members", status_code=303)


@app.post("/dashboard/add-contribution")
def add_contribution_form(member_id: int = Form(...), contribution_month: str = Form(...),
                           amount: float = Form(...), session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            if amount < float(settings["min_monthly_contribution"]):
                return RedirectResponse(
                    url=f"/dashboard?error=Amount+below+minimum+of+{settings['min_monthly_contribution']}",
                    status_code=303,
                )
            month_date = contribution_month if len(contribution_month) == 10 else f"{contribution_month}-01"
            cur.execute(
                "INSERT INTO contributions (member_id, contribution_month, amount, recorded_by) "
                "VALUES (%s, %s, %s, %s)",
                (member_id, month_date, amount, session_data["user_id"]),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/contributions/{contribution_id}/delete")
def delete_contribution(contribution_id: int, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/members?error=Only+the+chairperson+can+delete+a+contribution", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT member_id FROM contributions WHERE id = %s", (contribution_id,))
            row = cur.fetchone()
            if not row:
                return RedirectResponse(url="/members?error=Contribution+not+found", status_code=303)
            member_id = row["member_id"]
            cur.execute("DELETE FROM contributions WHERE id = %s", (contribution_id,))
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/statement?member_id={member_id}&notice=Contribution+deleted", status_code=303)


@app.get("/statement", response_class=HTMLResponse)
def statement_page(request: Request, member_id: Optional[int] = None,
                    session_data=Depends(get_session_optional)):
    if not session_data:
        return RedirectResponse(url="/login")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if session_data["role"] == "member":
                cur.execute("SELECT id FROM members WHERE user_id = %s", (session_data["user_id"],))
                own = cur.fetchone()
                if not own:
                    return HTMLResponse(
                        "<p style='font-family:sans-serif;padding:2rem'>"
                        "No member record is linked to your login yet - ask your treasurer to link it."
                        "</p>"
                    )
                mid = own["id"]
            else:
                if not member_id:
                    return RedirectResponse(url="/dashboard")
                mid = member_id

            cur.execute("SELECT * FROM members WHERE id = %s", (mid,))
            member = cur.fetchone()
            if not member:
                raise HTTPException(status_code=404, detail="Member not found")

            cur.execute(
                "SELECT id, contribution_month, amount FROM contributions "
                "WHERE member_id = %s ORDER BY contribution_month", (mid,)
            )
            contributions = cur.fetchall()
            total = sum((c["amount"] for c in contributions), Decimal("0"))

            cur.execute("SELECT * FROM loans WHERE member_id = %s ORDER BY issue_date DESC", (mid,))
            loans = cur.fetchall()
            settings = get_settings(cur)
            loan_details = [build_loan_detail(cur, loan, date.today(), settings["penalty_amount"]) for loan in loans]

            cur.execute(
                """SELECT d.dividend_amount, d.total_contribution, r.fiscal_year_label, r.computed_at
                   FROM dividends d JOIN dividend_runs r ON r.id = d.dividend_run_id
                   WHERE d.member_id = %s ORDER BY r.computed_at DESC""",
                (mid,),
            )
            dividends = cur.fetchall()

            cur.execute(
                "SELECT * FROM penalties WHERE member_id = %s ORDER BY waived ASC, created_at DESC", (mid,)
            )
            penalties = cur.fetchall()
            penalties_owed = sum((p["amount"] for p in penalties if not p["waived"] and not p["paid"]), Decimal("0"))

            cur.execute("SELECT COALESCE(SUM(amount),0) as total FROM contributions")
            group_total_contributions = cur.fetchone()["total"]

            standing = compute_contribution_standing(
                cur, mid, member["join_date"], settings["min_monthly_contribution"], date.today()
            )

            today = date.today()
            contribution_growth = compute_contribution_growth(cur, today)
            cur.execute("SELECT * FROM loans")
            all_loans_full = cur.fetchall()
            interest_growth = compute_interest_growth(all_loans_full, today)
            timely_payment_pct = compute_timely_payment_pct(all_loans_full, today)
    finally:
        conn.close()

    own_chart_json = json.dumps({
        "labels": [c["contribution_month"].strftime("%b %Y") for c in contributions],
        "data": [float(c["amount"]) for c in contributions],
    })

    return templates.TemplateResponse(request, "statement.html", {
        "member": member, "contributions": contributions,
        "total": total, "loans": loan_details, "dividends": dividends,
        "penalties": penalties, "penalties_owed": penalties_owed,
        "group_total_contributions": group_total_contributions,
        "contribution_growth": contribution_growth, "interest_growth": interest_growth,
        "timely_payment_pct": timely_payment_pct,
        "own_chart_json": own_chart_json, "greeting": time_greeting(),
        "standing": standing, "min_contribution": settings["min_monthly_contribution"],
        "role": session_data["role"],
    })


@app.post("/statement/email")
def email_statement(member_id: int = Form(...), recipient_email: str = Form(...),
                     session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            data = build_member_statement_data(cur, member_id, date.today())
    finally:
        conn.close()
    if not data:
        return RedirectResponse(url="/members?error=Member+not+found", status_code=303)

    body = render_statement_as_text(data)
    ok, err = send_email(recipient_email, f"GCSSHG statement - {data['member']['full_name']}", body)
    base = f"/statement?member_id={member_id}"
    if ok:
        return RedirectResponse(url=f"{base}&notice={quote_plus('Emailed to ' + recipient_email)}", status_code=303)
    return RedirectResponse(url=f"{base}&notice={quote_plus('Failed to send: ' + (err or 'unknown error'))}", status_code=303)
