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
    run_dividends, MemberContributionRecord,
    compute_due_date, is_loan_overdue, loan_overdue_months, loan_penalty_due,
    mid_tier_installment_schedule, LoanTerms, add_months,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/gcsshg")
SECRET_KEY = os.environ.get("SESSION_SECRET", "change-me-in-production")
SITE_URL = os.environ.get("SITE_URL", "http://127.0.0.1:8000")
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


def build_loan_detail(cur, loan, as_of, penalty_amount):
    """
    Computes repaid amount, current balance, overdue status, and penalty
    owed for a single loan - used consistently everywhere a loan is displayed.
    """
    terms = terms_from_loan_row(loan)
    override = loan.get("interest_override_periods")
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


class RepaymentCreate(BaseModel):
    loan_id: int
    amount: Decimal
    payment_date: Optional[date] = None


class DividendRunRequest(BaseModel):
    fiscal_year_label: str
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
            issue_date = payload.issue_date or date.today()
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

def compute_dividend_records(cur):
    cur.execute("SELECT id, full_name FROM members WHERE status = 'active'")
    members = cur.fetchall()
    records = []
    for m in members:
        cur.execute(
            "SELECT EXTRACT(MONTH FROM contribution_month)::int as mon, SUM(amount) as amt "
            "FROM contributions WHERE member_id = %s GROUP BY mon",
            (m["id"],),
        )
        rows = cur.fetchall()
        monthly = [(r["mon"], r["amt"]) for r in rows]
        records.append(MemberContributionRecord(member_id=m["id"], full_name=m["full_name"],
                                                  monthly_amounts=monthly))
    return records


def save_dividend_run(cur, fiscal_year_label, total_interest_pool, computed_by, results,
                       gross_pool=None, admin_amount=Decimal("0"), transaction_cost_amount=Decimal("0"),
                       admin_label="Administration", transaction_label="Transaction costs"):
    total_weighted = sum((r.weighted_contribution for r in results), Decimal("0"))
    cur.execute(
        """INSERT INTO dividend_runs (fiscal_year_label, total_interest_pool,
                                       total_weighted_contributions, computed_by,
                                       gross_pool, admin_amount, transaction_cost_amount,
                                       admin_label, transaction_label)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (fiscal_year_label, total_interest_pool, total_weighted, computed_by,
         gross_pool, admin_amount, transaction_cost_amount, admin_label, transaction_label),
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
            records = compute_dividend_records(cur)
            results = run_dividends(
                records, payload.total_interest_pool,
                settings["fiscal_year_start_month"], settings["fiscal_year_length_months"],
            )
            run_id = save_dividend_run(cur, payload.fiscal_year_label, payload.total_interest_pool,
                                        chair["user_id"], results)
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
            cur.execute(
                "SELECT COALESCE(SUM(interest_component),0) as total FROM loan_repayments"
            )
            total_interest_collected = cur.fetchone()["total"]
            cur.execute("SELECT COALESCE(SUM(dividend_amount),0) as total FROM dividends")
            total_already_paid = cur.fetchone()["total"]
    finally:
        conn.close()
    suggested_pool = total_interest_collected - total_already_paid
    return templates.TemplateResponse(request, "dividends.html", {
        "runs": runs, "suggested_pool": suggested_pool, "role": session_data["role"],
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
                       total_interest_pool: float = Form(...),
                       session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+run+dividends", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            gross = Decimal(str(total_interest_pool))
            admin_amount = (gross * settings["dividend_admin_percent"] / 100).quantize(Decimal("0.01"))
            transaction_amount = (gross * settings["dividend_transaction_percent"] / 100).quantize(Decimal("0.01"))
            member_pool = gross - admin_amount - transaction_amount
            records = compute_dividend_records(cur)
            results = run_dividends(
                records, member_pool,
                settings["fiscal_year_start_month"], settings["fiscal_year_length_months"],
            )
    finally:
        conn.close()
    results_sorted = sorted(results, key=lambda r: r.dividend_amount, reverse=True)
    return templates.TemplateResponse(request, "dividend_preview.html", {
        "results": results_sorted, "fiscal_year_label": fiscal_year_label,
        "total_interest_pool": total_interest_pool, "role": session_data["role"],
        "gross_pool": gross, "admin_amount": admin_amount,
        "transaction_amount": transaction_amount, "member_pool": member_pool,
        "admin_percent": settings["dividend_admin_percent"],
        "transaction_percent": settings["dividend_transaction_percent"],
        "admin_label": settings["dividend_admin_label"],
        "transaction_label": settings["dividend_transaction_label"],
    })


@app.post("/dividends/confirm")
def dividends_confirm(fiscal_year_label: str = Form(...), total_interest_pool: float = Form(...),
                       session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+run+dividends", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            gross = Decimal(str(total_interest_pool))
            admin_amount = (gross * settings["dividend_admin_percent"] / 100).quantize(Decimal("0.01"))
            transaction_amount = (gross * settings["dividend_transaction_percent"] / 100).quantize(Decimal("0.01"))
            member_pool = gross - admin_amount - transaction_amount
            records = compute_dividend_records(cur)
            results = run_dividends(
                records, member_pool,
                settings["fiscal_year_start_month"], settings["fiscal_year_length_months"],
            )
            run_id = save_dividend_run(cur, fiscal_year_label, member_pool,
                                        session_data["user_id"], results,
                                        gross_pool=gross, admin_amount=admin_amount,
                                        transaction_cost_amount=transaction_amount,
                                        admin_label=settings["dividend_admin_label"],
                                        transaction_label=settings["dividend_transaction_label"])
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/dividends/{run_id}", status_code=303)


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


@app.get("/bank-balance", response_class=HTMLResponse)
def bank_balance_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT b.*, u.full_name as recorded_by_name FROM bank_balance_log b
                   LEFT JOIN users u ON u.id = b.recorded_by ORDER BY b.recorded_at DESC"""
            )
            history = cur.fetchall()
    finally:
        conn.close()
    current_balance = history[0]["amount"] if history else Decimal("0")
    return templates.TemplateResponse(request, "bank_balance.html", {
        "history": history, "current_balance": current_balance, "role": session_data["role"],
    })


@app.post("/bank-balance/update")
def bank_balance_update(amount: float = Form(...), note: str = Form(""),
                         session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer"):
        return RedirectResponse(url="/bank-balance?error=Only+the+chairperson+or+treasurer+can+update+this", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bank_balance_log (amount, note, recorded_by) VALUES (%s, %s, %s)",
                (amount, note or None, session_data["user_id"]),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/bank-balance", status_code=303)


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "reports.html", {"role": session_data["role"]})


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
    })


@app.get("/reports/loans", response_class=HTMLResponse)
def loans_report(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            cur.execute(
                """SELECT l.*, m.full_name FROM loans l JOIN members m ON m.id = l.member_id
                   ORDER BY (l.status = 'active') DESC, l.issue_date DESC"""
            )
            loans = cur.fetchall()
            loan_rows = [build_loan_detail(cur, loan, date.today(), settings["penalty_amount"]) for loan in loans]
            total_principal = sum((l["principal"] for l in loan_rows), Decimal("0"))
            total_interest = sum(
                (l["current_balance"] - l["principal"] + l["amount_repaid"] for l in loan_rows), Decimal("0")
            )
            total_outstanding = sum((l["current_balance"] for l in loan_rows if l["status"] == "active"), Decimal("0"))
    finally:
        conn.close()
    return templates.TemplateResponse(request, "report_loans.html", {
        "loans": loan_rows, "total_principal": total_principal, "total_interest": total_interest,
        "total_outstanding": total_outstanding, "role": session_data["role"],
    })


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

            cur.execute(
                "SELECT COALESCE(SUM(dividend_amount),0) as total FROM dividends"
            )
            total_dividends_paid = cur.fetchone()["total"]

            cur.execute("SELECT COALESCE(SUM(principal),0) as total FROM loans")
            total_loans_issued = cur.fetchone()["total"]

            # Two angles on interest, both computed per-loan using each loan's own
            # stored terms (tier/rate/override), never re-derived from current settings:
            #
            # 1. Expected interest = what the interest calculator says has accrued
            #    across every loan ever issued, as of today (or as of the date a
            #    loan was cleared, so a settled loan doesn't keep "accruing" after
            #    it was actually paid off). This is total owed (principal+interest)
            #    minus total principal, i.e. purely the interest portion expected.
            #
            # 2. Actual (paid) interest = of the loans that have received at least
            #    one repayment (even partial), how much has actually come back in
            #    cash beyond their original principal. Untouched loans (zero
            #    repayments) are excluded entirely rather than dragging this
            #    figure negative by their full principal.
            cur.execute("SELECT * FROM loans")
            all_loans_full = cur.fetchall()
            expected_interest_total = Decimal("0")
            actual_repaid_touched = Decimal("0")
            actual_principal_touched = Decimal("0")
            for loan in all_loans_full:
                terms = terms_from_loan_row(loan)
                override = loan.get("interest_override_periods")
                as_of = loan["cleared_date"] if (loan["status"] == "cleared" and loan["cleared_date"]) else date.today()
                interest_due = loan_interest_due(loan["principal"], loan["issue_date"], as_of, terms, override)
                expected_interest_total += interest_due

                cur.execute(
                    "SELECT COALESCE(SUM(amount),0) as repaid FROM loan_repayments WHERE loan_id = %s",
                    (loan["id"],),
                )
                repaid = cur.fetchone()["repaid"]
                if repaid > 0:
                    actual_repaid_touched += repaid
                    actual_principal_touched += Decimal(loan["principal"])
            actual_interest_paid = actual_repaid_touched - actual_principal_touched

            settings = get_settings(cur)
            cur.execute("SELECT * FROM loans WHERE status = 'active'")
            active_loans = cur.fetchall()
            total_loans_outstanding = Decimal("0")
            overdue_count = 0
            for loan in active_loans:
                detail = build_loan_detail(cur, loan, date.today(), settings["penalty_amount"])
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
        "total_loans_outstanding": total_loans_outstanding,
        "total_loans_issued": total_loans_issued,
        "expected_interest_total": expected_interest_total,
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
                        join_date_field: str = Form(...), session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/members?error=Only+the+chairperson+can+edit+members", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE members SET full_name = %s, phone = %s, join_date = %s WHERE id = %s",
                (full_name, phone or None, date.fromisoformat(join_date_field), member_id),
            )
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

            # Stats always reflect the WHOLE group, regardless of search
            cur.execute(
                """SELECT l.*, m.full_name FROM loans l
                   JOIN members m ON m.id = l.member_id
                   ORDER BY (l.status = 'active') DESC, l.issue_date DESC"""
            )
            all_loans = cur.fetchall()
            all_loan_rows = [build_loan_detail(cur, loan, date.today(), settings["penalty_amount"]) for loan in all_loans]
            total_loans_issued = sum((Decimal(l["principal"]) for l in all_loans), Decimal("0"))
            total_repaid = sum((l["amount_repaid"] for l in all_loan_rows), Decimal("0"))
            total_outstanding = sum((l["current_balance"] for l in all_loan_rows if l["status"] == "active"), Decimal("0"))
            overdue_count = sum(1 for l in all_loan_rows if l["is_overdue"])

            # The table itself is filtered by search
            if q and q.strip():
                search_term = f"%{q.strip()}%"
                cur.execute(
                    """SELECT l.*, m.full_name FROM loans l
                       JOIN members m ON m.id = l.member_id
                       WHERE m.full_name ILIKE %s OR m.phone ILIKE %s
                       ORDER BY (l.status = 'active') DESC, l.issue_date DESC""",
                    (search_term, search_term),
                )
                loans = cur.fetchall()
                loan_rows = [build_loan_detail(cur, loan, date.today(), settings["penalty_amount"]) for loan in loans]
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
def repay_loan_page(loan_id: int, request: Request, session_data=Depends(get_session_optional)):
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
        "loan": detail, "role": session_data["role"],
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
    finally:
        conn.close()
    return templates.TemplateResponse(request, "loan_statement.html", {
        "loan": detail, "repayments": repayments, "schedule": schedule, "role": session_data["role"],
    })


@app.post("/dashboard/issue-loan")
def issue_loan_form(member_id: int = Form(...), principal: float = Form(...),
                     issue_date_field: str = Form(...),
                     interest_override_periods: Optional[str] = Form(None),
                     session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer"):
        return RedirectResponse(url="/loans?error=Only+the+chairperson+or+treasurer+can+issue+loans", status_code=303)
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
                """INSERT INTO loans (member_id, principal, issue_date, due_date, interest_tier,
                                       interest_rate, period_months, approved_by, interest_override_periods)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (member_id, principal, issue_date, due_date, terms.tier, terms.rate_percent,
                 terms.period_months, session_data["user_id"], override_val),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/loans/issue?success=Loan+issued+-+ready+for+the+next+one", status_code=303)


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


@app.post("/dashboard/repay-loan")
def repay_loan_form(loan_id: int = Form(...), amount: float = Form(...),
                     payment_date_field: str = Form(...), session_data=Depends(get_session_optional)):
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
            apply_loan_repayment(cur, loan, amount_dec, payment_date, session_data["user_id"])
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/loans/{loan_id}/statement?success=Repayment+recorded", status_code=303)


@app.post("/loans/bulk-repay")
async def bulk_repay_loans(request: Request, session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer"):
        return RedirectResponse(url="/loans?error=Only+the+chairperson+or+treasurer+can+record+repayments", status_code=303)
    form = await request.form()
    payment_date_field = form.get("payment_date_field", "").strip()
    if not payment_date_field:
        return RedirectResponse(url="/loans?error=Pick+a+payment+date", status_code=303)
    payment_date = date.fromisoformat(payment_date_field)
    search_query = form.get("search_query", "")

    conn = get_conn()
    saved = 0
    try:
        with conn.cursor() as cur:
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
                cur.execute("SELECT * FROM loans WHERE id = %s", (loan_id,))
                loan = cur.fetchone()
                if not loan:
                    continue
                apply_loan_repayment(cur, loan, amount_dec, payment_date, session_data["user_id"])
                saved += 1
            conn.commit()
    finally:
        conn.close()

    redirect_url = f"/loans?success={saved}+repayment(s)+recorded"
    if search_query:
        redirect_url += f"&q={search_query}"
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


@app.post("/admin/run-penalty-check")
def run_penalty_check(session_data=Depends(get_session_optional)):
    """
    Scans overdue loans and missed monthly contributions, inserting penalty
    records for any period not already charged. Safe to run repeatedly -
    the unique constraint on (member_id, penalty_type, reference_id, period_label)
    prevents double-charging the same month twice.
    """
    if not session_data or session_data["role"] not in ("chairperson", "treasurer"):
        return RedirectResponse(url="/penalties?error=Only+the+chairperson+or+treasurer+can+run+this+check", status_code=303)
    conn = get_conn()
    inserted = 0
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            penalty_amount = settings["penalty_amount"]
            today = date.today()

            # --- Overdue loan penalties ---
            cur.execute("SELECT * FROM loans WHERE status = 'active' AND due_date IS NOT NULL")
            loans = cur.fetchall()
            for loan in loans:
                cur.execute(
                    "SELECT COALESCE(SUM(amount),0) as repaid FROM loan_repayments WHERE loan_id = %s",
                    (loan["id"],),
                )
                repaid = cur.fetchone()["repaid"]
                terms = terms_from_loan_row(loan)
                balance = loan_balance_due(loan["principal"], repaid, loan["issue_date"], today, terms)
                if is_loan_overdue(loan["due_date"], balance, today):
                    months_overdue = loan_overdue_months(loan["due_date"], today)
                    for i in range(months_overdue):
                        period_month = add_months(loan["due_date"], i)
                        period_label = f"{period_month.year}-{period_month.month:02d}"
                        cur.execute(
                            """INSERT INTO penalties (member_id, penalty_type, reference_id, period_label, amount)
                               VALUES (%s, 'overdue_loan', %s, %s, %s)
                               ON CONFLICT (member_id, penalty_type, reference_id, period_label) DO NOTHING""",
                            (loan["member_id"], loan["id"], period_label, penalty_amount),
                        )
                        if cur.rowcount:
                            inserted += 1

            # --- Missed monthly contribution penalties ---
            # Cumulative check: a member is only penalized if their TOTAL contribution
            # to date falls short of (min_monthly_contribution x months elapsed since
            # joining). Someone who paid 1000 in March and 0 in April is NOT penalized -
            # their running total already covers both months. This avoids fining
            # members who front-load or pay unevenly but keep pace overall.
            cur.execute("SELECT id, join_date FROM members WHERE status = 'active'")
            members = cur.fetchall()
            current_month_start = today.replace(day=1)
            min_contribution = settings["min_monthly_contribution"]
            for m in members:
                standing = compute_contribution_standing(cur, m["id"], m["join_date"], min_contribution, today)
                if standing["is_current"]:
                    continue  # caught up overall - no penalty, regardless of which months were light

                shortfall_months = math.ceil(standing["shortfall"] / min_contribution)

                cur.execute(
                    "SELECT COUNT(*) as count FROM penalties WHERE member_id = %s AND penalty_type = 'missed_contribution'",
                    (m["id"],),
                )
                already_charged = cur.fetchone()["count"]

                for n in range(already_charged + 1, shortfall_months + 1):
                    period_label = f"shortfall-{n}"
                    cur.execute(
                        """INSERT INTO penalties (member_id, penalty_type, reference_id, period_label, amount)
                           VALUES (%s, 'missed_contribution', NULL, %s, %s)
                           ON CONFLICT (member_id, penalty_type, reference_id, period_label) DO NOTHING""",
                        (m["id"], period_label, penalty_amount),
                    )
                    if cur.rowcount:
                        inserted += 1
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/penalties?error=Penalty+check+complete+-+{inserted}+new+penalties+recorded", status_code=303)


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
                "SELECT contribution_month, amount FROM contributions "
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
        "own_chart_json": own_chart_json, "greeting": time_greeting(),
        "standing": standing, "min_contribution": settings["min_monthly_contribution"],
        "role": session_data["role"],
    })
