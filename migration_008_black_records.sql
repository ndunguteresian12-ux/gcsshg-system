-- Migration: adds proper tracking for loan defaults ("black records").
-- The 'defaulted' status was always a valid value conceptually but never
-- had a UI action or accountability trail - this adds that.
-- Run this in Neon's SQL Editor against your live database.

ALTER TABLE loans ADD COLUMN IF NOT EXISTS defaulted_at TIMESTAMP;
ALTER TABLE loans ADD COLUMN IF NOT EXISTS defaulted_by INT REFERENCES users(id);
ALTER TABLE loans ADD COLUMN IF NOT EXISTS default_reason TEXT;
