-- Migration 001 — Unique constraint on (company_name, stage)
--
-- Run this in the Supabase SQL Editor (Project → SQL Editor → New query →
-- paste → Run). Idempotent: the DO block skips the ALTER if the constraint
-- already exists, so safe to re-run.
--
-- Pre-check this against the live data first if you're unsure: the migration
-- will fail if any (company_name, stage) duplicates exist. Use this query:
--   select company_name, stage, count(*)
--   from public.deals
--   group by company_name, stage
--   having count(*) > 1;

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'deals_company_stage_uniq'
    ) then
        alter table public.deals
            add constraint deals_company_stage_uniq unique (company_name, stage);
    end if;
end$$;
