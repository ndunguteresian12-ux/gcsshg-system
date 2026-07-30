-- GCSSHG (Githiga Comprehensive School Self Help Group) database schema
-- Postgres. Mirrors the Elimu Hub pattern: simple relational tables,
-- RealDictCursor-friendly (plain columns, no exotic types).

-- ============================================================
-- GROUP SETTINGS (single row - group-wide rules, editable by chairperson)
-- ============================================================
CREATE TABLE IF NOT EXISTS group_settings (
    id SERIAL PRIMARY KEY,
    group_name TEXT NOT NULL DEFAULT 'Githiga Comprehensive School Self Help Group',
    min_monthly_contribution NUMERIC(12,2) NOT NULL DEFAULT 500.00,
    low_loan_ceiling NUMERIC(12,2) NOT NULL DEFAULT 19999.00,   -- loans <= this: monthly interest
    low_loan_interest_rate NUMERIC(5,2) NOT NULL DEFAULT 10.00, -- percent, per month
    high_loan_interest_rate NUMERIC(5,2) NOT NULL DEFAULT 10.00,-- percent, per 3-month period
    high_loan_period_months INT NOT NULL DEFAULT 3,
    -- fiscal_year_start_month: 1=Jan ... 12=Dec. Your sheet runs Mar -> Jan (11 months).
    fiscal_year_start_month INT NOT NULL DEFAULT 3,
    fiscal_year_length_months INT NOT NULL DEFAULT 11,
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

-- ============================================================
-- USERS (login accounts - chairperson, treasurer, secretary, members)
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    phone TEXT UNIQUE NOT NULL,           -- login identifier (M-Pesa-linked number)
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',  -- 'chairperson' | 'treasurer' | 'secretary' | 'member'
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

-- ============================================================
-- MEMBERS (a member may or may not have a login yet)
-- ============================================================
CREATE TABLE IF NOT EXISTS members (
    id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(id) ON DELETE SET NULL,
    full_name TEXT NOT NULL,
    phone TEXT,
    join_date DATE NOT NULL DEFAULT CURRENT_DATE,
    status TEXT NOT NULL DEFAULT 'active', -- 'active' | 'inactive' | 'exited'
    notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

-- ============================================================
-- CONTRIBUTIONS (one row per member per month; supports top-ups via multiple rows)
-- ============================================================
CREATE TABLE IF NOT EXISTS contributions (
    id SERIAL PRIMARY KEY,
    member_id INT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    contribution_month DATE NOT NULL,   -- always store as first-of-month, e.g. 2026-03-01
    amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
    recorded_by INT REFERENCES users(id),
    recorded_at TIMESTAMP NOT NULL DEFAULT now(),
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_contrib_member_month ON contributions(member_id, contribution_month);

-- ============================================================
-- LOANS
-- ============================================================
CREATE TABLE IF NOT EXISTS loans (
    id SERIAL PRIMARY KEY,
    member_id INT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    principal NUMERIC(12,2) NOT NULL CHECK (principal > 0),
    issue_date DATE NOT NULL DEFAULT CURRENT_DATE,
    interest_tier TEXT NOT NULL,      -- 'low' (<=19999, monthly) | 'high' (>=20000, quarterly)
    interest_rate NUMERIC(5,2) NOT NULL,     -- snapshot of rate at issue time, percent
    period_months INT NOT NULL,              -- 1 for low tier, 3 for high tier
    status TEXT NOT NULL DEFAULT 'active',    -- 'active' | 'cleared' | 'defaulted'
    approved_by INT REFERENCES users(id),
    cleared_date DATE,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

-- ============================================================
-- LOAN REPAYMENTS
-- ============================================================
CREATE TABLE IF NOT EXISTS loan_repayments (
    id SERIAL PRIMARY KEY,
    loan_id INT NOT NULL REFERENCES loans(id) ON DELETE CASCADE,
    payment_date DATE NOT NULL DEFAULT CURRENT_DATE,
    amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
    principal_component NUMERIC(12,2) NOT NULL DEFAULT 0,
    interest_component NUMERIC(12,2) NOT NULL DEFAULT 0,
    recorded_by INT REFERENCES users(id),
    recorded_at TIMESTAMP NOT NULL DEFAULT now()
);

-- ============================================================
-- DIVIDENDS (computed once per fiscal year, stored for audit trail)
-- ============================================================
CREATE TABLE IF NOT EXISTS dividend_runs (
    id SERIAL PRIMARY KEY,
    fiscal_year_label TEXT NOT NULL,       -- e.g. 'Mar 2025 - Jan 2026'
    total_interest_pool NUMERIC(14,2) NOT NULL,
    total_weighted_contributions NUMERIC(16,2) NOT NULL,
    computed_by INT REFERENCES users(id),
    computed_at TIMESTAMP NOT NULL DEFAULT now(),
    notes TEXT
);

CREATE TABLE IF NOT EXISTS dividends (
    id SERIAL PRIMARY KEY,
    dividend_run_id INT NOT NULL REFERENCES dividend_runs(id) ON DELETE CASCADE,
    member_id INT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    total_contribution NUMERIC(12,2) NOT NULL,   -- plain sum, for display
    weighted_contribution NUMERIC(16,2) NOT NULL, -- time-weighted, drives the payout
    dividend_amount NUMERIC(12,2) NOT NULL
);

-- ============================================================
-- AUDIT LOG (mirrors Elimu Hub's activity log pattern)
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(id),
    action TEXT NOT NULL,          -- e.g. 'RECORD_CONTRIBUTION', 'ISSUE_LOAN', 'RUN_DIVIDENDS'
    entity_type TEXT,
    entity_id INT,
    details JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

INSERT INTO group_settings (group_name) VALUES ('Githiga Comprehensive School Self Help Group')
ON CONFLICT DO NOTHING;
