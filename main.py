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
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List

from dotenv import load_dotenv
load_dotenv()  # reads .env in this folder so DATABASE_URL/SESSION_SECRET don't need manual `set`

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Depends, Cookie, Response, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
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
signer = itsdangerous.URLSafeTimedSerializer(SECRET_KEY)

app = FastAPI(title="GCSSHG Management System")
templates = Jinja2Templates(directory="templates")


def get_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def get_settings(cur):
    cur.execute("SELECT * FROM group_settings ORDER BY id LIMIT 1")
    return cur.fetchone()


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
    cur.execute(
        "SELECT COALESCE(SUM(amount), 0) as repaid FROM loan_repayments WHERE loan_id = %s",
        (loan["id"],),
    )
    repaid = cur.fetchone()["repaid"]
    balance = loan_balance_due(loan["principal"], repaid, loan["issue_date"], as_of, terms)
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

            return {
                "member": member,
                "total_contributed": total_contributed,
                "contributions": contributions,
                "loans": loan_details,
                "dividend_history": dividend_history,
            }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# LOANS
# ---------------------------------------------------------------------------

@app.post("/loans")
def issue_loan(payload: LoanCreate, officer=Depends(require_officer)):
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
                                       interest_rate, period_months, approved_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (payload.member_id, payload.principal, issue_date, due_date,
                 terms.tier, terms.rate_percent, terms.period_months, officer["user_id"]),
            )
            row = cur.fetchone()
            conn.commit()
            return row
    finally:
        conn.close()


@app.post("/loans/repayments")
def record_repayment(payload: RepaymentCreate, officer=Depends(require_officer)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM loans WHERE id = %s", (payload.loan_id,))
            loan = cur.fetchone()
            if not loan:
                raise HTTPException(status_code=404, detail="Loan not found")
            terms = terms_from_loan_row(loan)

            payment_date = payload.payment_date or date.today()
            interest_due = loan_interest_due(loan["principal"], loan["issue_date"], payment_date, terms)
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
            full_balance = loan_balance_due(loan["principal"], total_repaid, loan["issue_date"], payment_date, terms)
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

@app.post("/dividends/run")
def run_dividend_calculation(payload: DividendRunRequest, chair=Depends(require_chairperson)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
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

            results = run_dividends(
                records, payload.total_interest_pool,
                settings["fiscal_year_start_month"], settings["fiscal_year_length_months"],
            )
            total_weighted = sum((r.weighted_contribution for r in results), Decimal("0"))

            cur.execute(
                """INSERT INTO dividend_runs (fiscal_year_label, total_interest_pool,
                                               total_weighted_contributions, computed_by)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (payload.fiscal_year_label, payload.total_interest_pool, total_weighted, chair["user_id"]),
            )
            run_id = cur.fetchone()["id"]

            for r in results:
                cur.execute(
                    """INSERT INTO dividends (dividend_run_id, member_id, total_contribution,
                                               weighted_contribution, dividend_amount)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (run_id, r.member_id, r.total_contribution, r.weighted_contribution, r.dividend_amount),
                )
            conn.commit()
            return {
                "dividend_run_id": run_id,
                "results": [r.__dict__ for r in results],
            }
    finally:
        conn.close()


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
            cur.execute("SELECT id, full_name FROM members ORDER BY full_name")
            members = cur.fetchall()

            cur.execute("SELECT COUNT(*) as count FROM members")
            member_count = cur.fetchone()["count"]

            cur.execute("SELECT COALESCE(SUM(amount),0) as total FROM contributions")
            total_contributions = cur.fetchone()["total"]

            cur.execute(
                "SELECT COALESCE(SUM(dividend_amount),0) as total FROM dividends"
            )
            total_dividends_paid = cur.fetchone()["total"]

            cur.execute(
                "SELECT COALESCE(SUM(interest_component),0) as total FROM loan_repayments"
            )
            total_interest_earned = cur.fetchone()["total"]

            cur.execute("SELECT COALESCE(SUM(principal),0) as total FROM loans")
            total_loans_issued = cur.fetchone()["total"]

            settings = get_settings(cur)
            cur.execute("SELECT * FROM loans WHERE status = 'active'")
            active_loans = cur.fetchall()
            total_loans_outstanding = Decimal("0")
            for loan in active_loans:
                detail = build_loan_detail(cur, loan, date.today(), settings["penalty_amount"])
                total_loans_outstanding += detail["current_balance"]
    finally:
        conn.close()
    return templates.TemplateResponse(request, "dashboard.html", {
        "members": members, "role": session_data["role"],
        "member_count": member_count,
        "total_contributions": total_contributions,
        "total_loans_outstanding": total_loans_outstanding,
        "total_loans_issued": total_loans_issued,
        "total_interest_earned": total_interest_earned,
        "total_dividends_paid": total_dividends_paid,
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
                """SELECT m.*, COALESCE(SUM(c.amount), 0) as total_contributed
                   FROM members m
                   LEFT JOIN contributions c ON c.member_id = m.id
                   GROUP BY m.id ORDER BY m.full_name"""
            )
            members = cur.fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "members.html", {
        "members": members, "role": session_data["role"],
    })


@app.get("/loans", response_class=HTMLResponse)
def loans_page(request: Request, session_data=Depends(get_session_optional)):
    if not session_data:
        return RedirectResponse(url="/login")
    if session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/statement")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            settings = get_settings(cur)
            cur.execute("SELECT id, full_name FROM members ORDER BY full_name")
            members = cur.fetchall()

            cur.execute(
                """SELECT l.*, m.full_name FROM loans l
                   JOIN members m ON m.id = l.member_id
                   ORDER BY (l.status = 'active') DESC, l.issue_date DESC"""
            )
            loans = cur.fetchall()
            loan_rows = [build_loan_detail(cur, loan, date.today(), settings["penalty_amount"]) for loan in loans]

            total_loans_issued = sum((Decimal(l["principal"]) for l in loans), Decimal("0"))
            total_outstanding = sum((l["current_balance"] for l in loan_rows if l["status"] == "active"), Decimal("0"))
            overdue_count = sum(1 for l in loan_rows if l["is_overdue"])
    finally:
        conn.close()
    return templates.TemplateResponse(request, "loans.html", {
        "members": members, "loans": loan_rows, "role": session_data["role"],
        "total_loans_issued": total_loans_issued, "total_outstanding": total_outstanding,
        "overdue_count": overdue_count, "high_loan_ceiling": settings["high_loan_ceiling"],
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
                     issue_date_field: str = Form(...), session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
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
                                       interest_rate, period_months, approved_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (member_id, principal, issue_date, due_date, terms.tier, terms.rate_percent,
                 terms.period_months, session_data["user_id"]),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/loans", status_code=303)


@app.post("/dashboard/repay-loan")
def repay_loan_form(loan_id: int = Form(...), amount: float = Form(...),
                     payment_date_field: str = Form(...), session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM loans WHERE id = %s", (loan_id,))
            loan = cur.fetchone()
            if not loan:
                return RedirectResponse(url="/loans?error=Loan+not+found", status_code=303)
            terms = terms_from_loan_row(loan)

            payment_date = date.fromisoformat(payment_date_field)
            amount_dec = Decimal(str(amount))
            interest_due = loan_interest_due(loan["principal"], loan["issue_date"], payment_date, terms)
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
                (loan_id, payment_date, amount_dec, principal_component, interest_component,
                 session_data["user_id"]),
            )

            cur.execute(
                "SELECT COALESCE(SUM(amount),0) as total_repaid FROM loan_repayments WHERE loan_id = %s",
                (loan_id,),
            )
            total_repaid = cur.fetchone()["total_repaid"]
            full_balance = loan_balance_due(loan["principal"], total_repaid, loan["issue_date"], payment_date, terms)
            if full_balance <= 0:
                cur.execute("UPDATE loans SET status = 'cleared', cleared_date = %s WHERE id = %s",
                            (payment_date, loan_id))
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/loans", status_code=303)


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


@app.post("/loans/{loan_id}/edit")
def edit_loan(loan_id: int, principal: float = Form(...), issue_date_field: str = Form(...),
              session_data=Depends(get_session_optional)):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/loans?error=Only+the+chairperson+can+edit+loans", status_code=303)
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
                   interest_tier = %s, interest_rate = %s, period_months = %s WHERE id = %s""",
                (principal, issue_date, due_date, terms.tier, terms.rate_percent,
                 terms.period_months, loan_id),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/loans", status_code=303)


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
    session_data=Depends(get_session_optional),
):
    if not session_data or session_data["role"] != "chairperson":
        return RedirectResponse(url="/dashboard?error=Only+the+chairperson+can+change+settings", status_code=303)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE group_settings SET
                   min_monthly_contribution = %s, low_loan_ceiling = %s, high_loan_ceiling = %s,
                   low_loan_interest_rate = %s, high_loan_interest_rate = %s,
                   low_loan_deadline_months = %s, high_loan_deadline_months = %s,
                   penalty_amount = %s, updated_at = now()""",
                (min_monthly_contribution, low_loan_ceiling, high_loan_ceiling,
                 low_loan_interest_rate, high_loan_interest_rate,
                 low_loan_deadline_months, high_loan_deadline_months, penalty_amount),
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
    if not session_data or session_data["role"] not in ("chairperson", "treasurer", "secretary"):
        return RedirectResponse(url="/login")
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
            cur.execute("SELECT id, join_date FROM members WHERE status = 'active'")
            members = cur.fetchall()
            current_month_start = today.replace(day=1)
            for m in members:
                cursor_month = m["join_date"].replace(day=1)
                while cursor_month < current_month_start:  # only fully-completed months
                    cur.execute(
                        "SELECT COALESCE(SUM(amount),0) as total FROM contributions "
                        "WHERE member_id = %s AND contribution_month = %s",
                        (m["id"], cursor_month),
                    )
                    total = cur.fetchone()["total"]
                    if total < settings["min_monthly_contribution"]:
                        period_label = f"{cursor_month.year}-{cursor_month.month:02d}"
                        cur.execute(
                            """INSERT INTO penalties (member_id, penalty_type, reference_id, period_label, amount)
                               VALUES (%s, 'missed_contribution', NULL, %s, %s)
                               ON CONFLICT (member_id, penalty_type, reference_id, period_label) DO NOTHING""",
                            (m["id"], period_label, penalty_amount),
                        )
                        if cur.rowcount:
                            inserted += 1
                    cursor_month = add_months(cursor_month, 1)
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
                   ORDER BY p.waived ASC, p.created_at DESC"""
            )
            penalties = cur.fetchall()
            total_outstanding = sum(
                (Decimal(p["amount"]) for p in penalties if not p["waived"]), Decimal("0")
            )
    finally:
        conn.close()
    return templates.TemplateResponse(request, "penalties.html", {
        "penalties": penalties, "total_outstanding": total_outstanding, "role": session_data["role"],
    })


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
            loan_details = []
            for loan in loans:
                cur.execute(
                    "SELECT COALESCE(SUM(amount),0) as repaid FROM loan_repayments WHERE loan_id = %s",
                    (loan["id"],),
                )
                repaid = cur.fetchone()["repaid"]
                balance = loan_balance_due(loan["principal"], repaid, loan["issue_date"], date.today())
                loan_details.append({**loan, "amount_repaid": repaid, "current_balance": balance})

            cur.execute(
                """SELECT d.dividend_amount, d.total_contribution, r.fiscal_year_label, r.computed_at
                   FROM dividends d JOIN dividend_runs r ON r.id = d.dividend_run_id
                   WHERE d.member_id = %s ORDER BY r.computed_at DESC""",
                (mid,),
            )
            dividends = cur.fetchall()
    finally:
        conn.close()

    return templates.TemplateResponse(request, "statement.html", {
        "member": member, "contributions": contributions,
        "total": total, "loans": loan_details, "dividends": dividends,
        "role": session_data["role"],
    })
