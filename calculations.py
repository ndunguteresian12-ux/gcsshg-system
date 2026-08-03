"""
GCSSHG core calculation engine.

Three things have to be exactly right, everything else in the app is CRUD:
  1. Loan interest (tiered: <20,000 = 10%/month, 20,000-30,000 = flat 10%
     total over a 3-month installment schedule)
  2. Due dates and overdue/penalty tracking
  3. Dividend allocation (time-weighted by month contributed)

Kept as pure functions with no DB/FastAPI dependency so they can be
unit-tested in isolation and reused from scripts as well as the API.
"""

from dataclasses import dataclass
from datetime import date
from calendar import monthrange
from math import ceil
from decimal import Decimal, ROUND_HALF_UP


# ---------------------------------------------------------------------------
# LOAN INTEREST + TERMS
# ---------------------------------------------------------------------------

LOW_LOAN_CEILING = Decimal("19999")
HIGH_LOAN_CEILING = Decimal("30000")  # informational ceiling - loans above this still processed under mid-tier terms, flagged in UI

LOW_LOAN_RATE = Decimal("10")     # percent, per 1 month, recurring
MID_LOAN_RATE = Decimal("10")     # percent, FLAT one-time total over the loan's 3-month term

LOW_LOAN_DEADLINE_MONTHS = 1
MID_LOAN_DEADLINE_MONTHS = 3

DEFAULT_PENALTY_AMOUNT = Decimal("50")  # KES, editable via group_settings


@dataclass
class LoanTerms:
    tier: str            # 'low' | 'mid'
    rate_percent: Decimal
    period_months: int      # billing period unit (1 for low; used for low-tier recurring calc)
    deadline_months: int    # months allowed before the loan is "due"
    exceeds_ceiling: bool = False  # True if principal > HIGH_LOAN_CEILING (mid-tier terms applied anyway)


def classify_loan(principal: Decimal,
                   low_ceiling: Decimal = LOW_LOAN_CEILING,
                   low_rate: Decimal = LOW_LOAN_RATE,
                   mid_rate: Decimal = MID_LOAN_RATE,
                   low_deadline: int = LOW_LOAN_DEADLINE_MONTHS,
                   mid_deadline: int = MID_LOAN_DEADLINE_MONTHS,
                   high_ceiling: Decimal = HIGH_LOAN_CEILING) -> LoanTerms:
    """Determine which interest tier a loan falls into, per GCSSHG rules."""
    principal = Decimal(principal)
    if principal <= low_ceiling:
        return LoanTerms(tier="low", rate_percent=low_rate, period_months=1,
                          deadline_months=low_deadline, exceeds_ceiling=False)
    return LoanTerms(tier="mid", rate_percent=mid_rate, period_months=3,
                      deadline_months=mid_deadline, exceeds_ceiling=(principal > high_ceiling))


def add_months(d: date, months: int) -> date:
    """Add whole calendar months to a date, clamping the day to the target month's length."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def compute_due_date(issue_date: date, terms: LoanTerms) -> date:
    return add_months(issue_date, terms.deadline_months)


def months_between(start: date, end: date) -> int:
    """Whole calendar months between two dates (ignores day-of-month direction)."""
    return (end.year - start.year) * 12 + (end.month - start.month)


def billing_periods_elapsed(issue_date: date, as_of: date, period_months: int) -> int:
    """
    Number of billing periods since issue for the LOW tier's recurring
    monthly interest. Stepping into a new period at all triggers that
    period's full interest charge - no daily proration.
    """
    if as_of <= issue_date:
        return 0
    total_months = months_between(issue_date, as_of)
    if as_of.day > issue_date.day:
        total_months += 1
    total_months = max(total_months, 1)
    return ceil(total_months / period_months)


def loan_interest_due(principal: Decimal, issue_date: date, as_of: date, terms: LoanTerms = None) -> Decimal:
    """
    Total interest owed on a loan.
    - low tier: 10% per elapsed month, keeps accruing indefinitely if unpaid (unchanged behavior)
    - mid tier: FLAT 10% of original principal, one-time - does not grow with lateness
      (lateness cost is the separate overdue penalty, not extra interest)
    """
    principal = Decimal(principal)
    terms = terms or classify_loan(principal)
    if terms.tier == "low":
        periods = billing_periods_elapsed(issue_date, as_of, terms.period_months)
        interest = principal * (terms.rate_percent / Decimal("100")) * periods
    else:
        interest = principal * (terms.rate_percent / Decimal("100"))
    return interest.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def loan_balance_due(principal: Decimal, amount_repaid: Decimal, issue_date: date, as_of: date,
                      terms: LoanTerms = None) -> Decimal:
    """Outstanding balance = principal + accrued interest - repayments made so far."""
    terms = terms or classify_loan(principal)
    total_owed = Decimal(principal) + loan_interest_due(principal, issue_date, as_of, terms)
    return (total_owed - Decimal(amount_repaid)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def mid_tier_installment_schedule(principal: Decimal, issue_date: date, terms: LoanTerms = None):
    """
    Returns the 3 suggested monthly installments for a mid-tier loan:
    each is (principal/3) + (total_interest/3), for display/reference only -
    actual repayments are still tracked freely, this is just the expected schedule.
    """
    terms = terms or classify_loan(principal)
    principal = Decimal(principal)
    total_interest = principal * (terms.rate_percent / Decimal("100"))
    monthly_principal = (principal / 3).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    monthly_interest = (total_interest / 3).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    schedule = []
    for i in range(1, 4):
        due = add_months(issue_date, i)
        schedule.append({
            "installment_number": i,
            "due_date": due,
            "principal_component": monthly_principal,
            "interest_component": monthly_interest,
            "total": monthly_principal + monthly_interest,
        })
    return schedule


# ---------------------------------------------------------------------------
# OVERDUE + PENALTIES
# ---------------------------------------------------------------------------

def is_loan_overdue(due_date: date, balance: Decimal, as_of: date) -> bool:
    return balance > 0 and as_of > due_date


def loan_overdue_months(due_date: date, as_of: date) -> int:
    """How many full months past the due date (minimum 1 if overdue at all)."""
    if as_of <= due_date:
        return 0
    total_months = months_between(due_date, as_of)
    if as_of.day > due_date.day:
        total_months += 1
    return max(total_months, 1)


def loan_penalty_due(due_date: date, balance: Decimal, as_of: date, penalty_amount: Decimal) -> Decimal:
    if not is_loan_overdue(due_date, balance, as_of):
        return Decimal("0")
    months = loan_overdue_months(due_date, as_of)
    return (Decimal(penalty_amount) * months).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# TIME-WEIGHTED DIVIDENDS
# ---------------------------------------------------------------------------

def fiscal_month_sequence(start_month: int, length: int):
    """
    Returns a list of `length` calendar month numbers starting at start_month,
    wrapping around the year. E.g. start=3, length=11 -> [3,4,5,6,7,8,9,10,11,12,1]
    """
    return [((start_month - 1 + i) % 12) + 1 for i in range(length)]


def month_weight(month_index_in_year: int, fiscal_length: int) -> int:
    """Weight for a contribution made in the Nth month of the fiscal year (1-indexed)."""
    return fiscal_length - month_index_in_year + 1


@dataclass
class MemberContributionRecord:
    member_id: int
    full_name: str
    monthly_amounts: list  # list[tuple[int, Decimal]] - (calendar_month_number, amount)


def compute_weighted_contribution(record: MemberContributionRecord, start_month: int, fiscal_length: int) -> Decimal:
    seq = fiscal_month_sequence(start_month, fiscal_length)
    month_position = {m: idx + 1 for idx, m in enumerate(seq)}
    weighted_total = Decimal("0")
    for cal_month, amount in record.monthly_amounts:
        pos = month_position[cal_month]
        w = month_weight(pos, fiscal_length)
        weighted_total += Decimal(amount) * w
    return weighted_total


@dataclass
class DividendResult:
    member_id: int
    full_name: str
    total_contribution: Decimal
    weighted_contribution: Decimal
    dividend_amount: Decimal


def run_dividends(records: list, total_interest_pool: Decimal, start_month: int, fiscal_length: int) -> list:
    weighted = []
    total_weighted = Decimal("0")
    for r in records:
        w = compute_weighted_contribution(r, start_month, fiscal_length)
        weighted.append(w)
        total_weighted += w

    results = []
    pool = Decimal(total_interest_pool)
    for r, w in zip(records, weighted):
        share = (w / total_weighted) if total_weighted > 0 else Decimal("0")
        dividend = (pool * share).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_contribution = sum(Decimal(a) for _, a in r.monthly_amounts)
        results.append(DividendResult(
            member_id=r.member_id, full_name=r.full_name,
            total_contribution=total_contribution, weighted_contribution=w,
            dividend_amount=dividend,
        ))
    return results


if __name__ == "__main__":
    print("Low tier: 15,000 loan issued Jan 1, checked May 5 (well past 1-month due date)")
    terms_low = classify_loan(Decimal("15000"))
    due = compute_due_date(date(2026, 1, 1), terms_low)
    print("  due date:", due)
    print("  interest due:", loan_interest_due(Decimal("15000"), date(2026, 1, 1), date(2026, 5, 5), terms_low))
    print("  overdue?", is_loan_overdue(due, Decimal("15000"), date(2026, 5, 5)))
    print("  overdue months:", loan_overdue_months(due, date(2026, 5, 5)))
    print("  penalty due:", loan_penalty_due(due, Decimal("15000"), date(2026, 5, 5), Decimal("50")))

    print("\nMid tier: 25,000 loan issued Jan 1, checked at month 2 (not yet due)")
    terms_mid = classify_loan(Decimal("25000"))
    due2 = compute_due_date(date(2026, 1, 1), terms_mid)
    print("  due date:", due2)
    print("  interest due (should be flat 2500, not growing):",
          loan_interest_due(Decimal("25000"), date(2026, 1, 1), date(2026, 3, 1), terms_mid))
    print("  installment schedule:")
    for inst in mid_tier_installment_schedule(Decimal("25000"), date(2026, 1, 1), terms_mid):
        print("   ", inst)

    print("\nMid tier: same loan, checked well past due date (month 5, unpaid)")
    print("  interest due (still flat 2500):",
          loan_interest_due(Decimal("25000"), date(2026, 1, 1), date(2026, 6, 1), terms_mid))
    print("  overdue?", is_loan_overdue(due2, Decimal("25000"), date(2026, 6, 1)))
    print("  penalty due:", loan_penalty_due(due2, Decimal("25000"), date(2026, 6, 1), Decimal("50")))
