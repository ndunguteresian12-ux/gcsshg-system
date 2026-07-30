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
from fastapi import FastAPI, HTTPException, Depends, Cookie, Response
from pydantic import BaseModel
import bcrypt
import itsdangerous

from calculations import (
    classify_loan, loan_interest_due, loan_balance_due,
    run_dividends, MemberContributionRecord,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/gcsshg")
SECRET_KEY = os.environ.get("SESSION_SECRET", "change-me-in-production")
signer = itsdangerous.URLSafeTimedSerializer(SECRET_KEY)

app = FastAPI(title="GCSSHG Management System")


def get_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def get_settings(cur):
    cur.execute("SELECT * FROM group_settings ORDER BY id LIMIT 1")
    return cur.fetchone()


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
def create_member(payload: MemberCreate, officer=Depends(require_officer)):
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


@app.get("/members")
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
            loan_details = []
            for loan in loans:
                cur.execute(
                    "SELECT COALESCE(SUM(amount), 0) as repaid FROM loan_repayments WHERE loan_id = %s",
                    (loan["id"],),
                )
                repaid = cur.fetchone()["repaid"]
                balance = loan_balance_due(loan["principal"], repaid, loan["issue_date"], date.today())
                loan_details.append({**loan, "amount_repaid": repaid, "current_balance": balance})

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
    terms = classify_loan(payload.principal)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO loans (member_id, principal, issue_date, interest_tier, interest_rate,
                                       period_months, approved_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (payload.member_id, payload.principal, payload.issue_date or date.today(),
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

            payment_date = payload.payment_date or date.today()
            interest_due = loan_interest_due(loan["principal"], loan["issue_date"], payment_date)
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

            balance = loan_balance_due(loan["principal"],
                                        principal_component,  # only this payment's principal reduces balance check below
                                        loan["issue_date"], payment_date)
            # recompute full balance including all historical repayments
            cur.execute(
                "SELECT COALESCE(SUM(amount),0) as total_repaid FROM loan_repayments WHERE loan_id = %s",
                (payload.loan_id,),
            )
            total_repaid = cur.fetchone()["total_repaid"]
            full_balance = loan_balance_due(loan["principal"], total_repaid, loan["issue_date"], payment_date)
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
