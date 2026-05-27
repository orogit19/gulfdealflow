-- Migration 004 - AI extraction fields and statuses.
--
-- Run this in the Supabase SQL Editor after migration 003. Idempotent:
-- columns are added only when missing, and status checks are refreshed.

alter table public.raw_sources
    drop constraint if exists raw_sources_status_chk;

alter table public.raw_sources
    add constraint raw_sources_status_chk
        check (
            status in (
                'pending',
                'fetched',
                'fetch_failed',
                'extraction_failed',
                'extracted',
                'not_a_funding_round'
            )
        );

alter table public.extracted_deals
    add column if not exists announcement_date text,
    add column if not exists sector text,
    add column if not exists sub_sector text,
    add column if not exists website text,
    add column if not exists is_funding_round boolean,
    add column if not exists confidence_score numeric,
    add column if not exists extraction_notes text;

alter table public.extracted_deals
    drop constraint if exists extracted_deals_status_chk;

alter table public.extracted_deals
    add constraint extracted_deals_status_chk
        check (
            extraction_status in (
                'pending',
                'extracted',
                'not_a_funding_round',
                'reviewed',
                'rejected',
                'extraction_failed'
            )
        );
