"""
GCSSHG core calculation engine.

Two things have to be exactly right, everything else in the app is CRUD:
  1. Loan interest (tiered: <20,000 = 10%/month, >=20,000 = 10%/3 months)
  2. Dividend allocation (time-weighted by month contributed)

Kept as pure functions with no DB/FastAPI dependency so they can be
unit-tested in isolation and reused from scripts (e.g. a one-off
year-end dividend run) as well as from the API.
"""

from dataclasses import dataclass
from datetime import date
from calendar import monthrange
from math import ceil
from decimal import Decimal, ROUND_HALF_UP


# ---------------------------------------------------------------------------
# LOAN INTEREST
# ---------------------------------------------------------------------------

LOW_LOAN_CEILING = Decimal("19999")
LOW_LOAN_RATE = Decimal("10")     # percent, per 1 month
HIGH_LOAN_RATE = Decimal("10")    # percent, per 3 months
HIGH_LOAN_PERIOD_MONTHS = 3


@dataclass
class LoanTerms:
    tier: str            # 'low' | 'high'
    rate_percent: Decimal
    period_months: int


def classify_loan(principal: Decimal) -> LoanTerms:
    """Determine which interest tier a loan falls into, per GCSSHG rules."""
    principal = Decimal(principal)
    if principal <= LOW_LOAN_CEILING:
        return LoanTerms(tier="low", rate_percent=LOW_LOAN_RATE, period_months=1)
    return LoanTerms(tier="high", rate_percent=HIGH_LOAN_RATE, period_months=HIGH_LOAN_PERIOD_MONTHS)


def months_between(start: date, end: date) -> int:
    """Whole calendar months between two dates (ignores day-of-month direction)."""
    return (end.year - start.year) * 12 + (end.month - start.month)


def billing_periods_elapsed(issue_date: date, as_of: date, period_months: int) -> int:
    """
    Number of *complete or partially-entered* billing periods since issue.
    GCSSHG practice (like most table-banking groups): stepping into a new
    period at all triggers that period's full interest charge - there's no
    daily proration. So we round UP to the next whole period.
    """
    if as_of <= issue_date:
        return 0
    total_months = months_between(issue_date, as_of)
    # +1 day rolled into a new month still counts as having entered that period
    if as_of.day > issue_date.day:
        total_months += 1
    total_months = max(total_months, 1)  # any elapsed time at all = at least 1 period charged
    return ceil(total_months / period_months)


def loan_interest_due(principal: Decimal, issue_date: date, as_of: date) -> Decimal:
    """
    Total interest accrued on a loan (simple interest per period, GCSSHG-style:
    each period charges rate% of the ORIGINAL principal - not compounding,
    consistent with how the group currently calculates by hand).
    """
    principal = Decimal(principal)
    terms = classify_loan(principal)
    periods = billing_periods_elapsed(issue_date, as_of, terms.period_months)
    interest = principal * (terms.rate_percent / Decimal("100")) * periods
    return interest.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def loan_balance_due(principal: Decimal, amount_repaid: Decimal, issue_date: date, as_of: date) -> Decimal:
    """Outstanding balance = principal + accrued interest - repayments made so far."""
    total_owed = Decimal(principal) + loan_interest_due(principal, issue_date, as_of)
    return (total_owed - Decimal(amount_repaid)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# TIME-WEIGHTED DIVIDENDS
# ---------------------------------------------------------------------------

def fiscal_month_sequence(start_month: int, length: int):
    """
    Returns a list of `length` calendar month numbers starting at start_month,
    wrapping around the year. E.g. start=3, length=11 -> [3,4,5,6,7,8,9,10,11,12,1]
    (Mar through Jan) - matches your sheet's MAR..JAN2 columns.
    """
    return [((start_month - 1 + i) % 12) + 1 for i in range(length)]


def month_weight(month_index_in_year: int, fiscal_length: int) -> int:
    """
    Weight for a contribution made in the Nth month of the fiscal year
    (1-indexed). A contribution in month 1 (e.g. March) sits in the pool
    for the whole fiscal_length months and gets full weight; a contribution
    in the last month gets weight 1.
    """
    return fiscal_length - month_index_in_year + 1


@dataclass
class MemberContributionRecord:
    member_id: int
    full_name: str
    # ordered list of (calendar_month_number, amount) for the fiscal year
    monthly_amounts: list  # list[tuple[int, Decimal]]


def compute_weighted_contribution(record: MemberContributionRecord, start_month: int, fiscal_length: int) -> Decimal:
    seq = fiscal_month_sequence(start_month, fiscal_length)
    month_position = {m: idx + 1 for idx, m in enumerate(seq)}  # calendar month -> position 1..N
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


def run_dividends(
    records: list,               # list[MemberContributionRecord]
    total_interest_pool: Decimal,
    start_month: int,
    fiscal_length: int,
) -> list:
    """
    Allocates the year's total interest pool across members proportional
    to their time-weighted contributions.
    """
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
            member_id=r.member_id,
            full_name=r.full_name,
            total_contribution=total_contribution,
            weighted_contribution=w,
            dividend_amount=dividend,
        ))
    return results


# ---------------------------------------------------------------------------
# Quick self-test using a couple of rows shaped like your sheet
# (Mar..Jan = 11 months). Run: python3 calculations.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Two members: one who front-loaded contributions early in the year,
    # one who paid the same TOTAL but mostly late - front-loader should
    # get a bigger dividend despite equal totals.
    front_loader = MemberContributionRecord(
        member_id=1, full_name="Early Payer",
        monthly_amounts=[(3, 2000), (4, 0), (5, 0), (6, 0), (7, 0), (8, 0),
                          (9, 0), (10, 0), (11, 0), (12, 0), (1, 0)]
    )
    late_payer = MemberContributionRecord(
        member_id=2, full_name="Late Payer",
        monthly_amounts=[(3, 0), (4, 0), (5, 0), (6, 0), (7, 0), (8, 0),
                          (9, 0), (10, 0), (11, 0), (12, 0), (1, 2000)]
    )
    results = run_dividends(
        [front_loader, late_payer],
        total_interest_pool=Decimal("1100"),
        start_month=3, fiscal_length=11,
    )
    for r in results:
        print(f"{r.full_name}: total={r.total_contribution} weighted={r.weighted_contribution} dividend={r.dividend_amount}")

    print()
    print("Loan interest checks:")
    # Low tier: 15,000 loan, entering its 3rd monthly period -> 10% x 3 = 4500 interest
    print("15,000 loan, Jan 1 -> Mar 5:",
          loan_interest_due(Decimal("15000"), date(2026, 1, 1), date(2026, 3, 5)))
    # High tier: 25,000 loan, entering its 2nd quarterly period -> 10% x2 = 5000 interest
    print("25,000 loan, Jan 1 -> May 2:",
          loan_interest_due(Decimal("25000"), date(2026, 1, 1), date(2026, 5, 2)))
