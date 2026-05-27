-- Migration 002 — Widen the unique constraint from (company_name, stage)
-- to (company_name, stage, date).
--
-- Background: a company can legitimately raise multiple rounds at the same
-- stage (e.g. Seed + Seed extension, two Series Bs years apart). Migration
-- 001's `(company_name, stage)` constraint was too tight to allow that. The
-- triple `(company_name, stage, date)` permits multiple same-stage rounds
-- as long as the announcement dates differ.
--
-- Idempotent: drops the old constraint if present, adds the new one only if
-- it doesn't already exist. Safe to re-run.

do $$
begin
    -- Drop the previous tighter constraint, if it still exists.
    alter table public.deals
        drop constraint if exists deals_company_stage_uniq;

    -- Add the new triple-uniqueness, only if not already present.
    if not exists (
        select 1 from pg_constraint where conname = 'deals_company_stage_date_uniq'
    ) then
        alter table public.deals
            add constraint deals_company_stage_date_uniq
            unique (company_name, stage, date);
    end if;
end$$;
