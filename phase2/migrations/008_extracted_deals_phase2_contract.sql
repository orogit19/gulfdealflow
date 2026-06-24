-- Migration 008 - Phase 2 structured draft extracted_deals contract.
--
-- Keeps extracted_deals as a review table only. Nothing here writes to the
-- canonical public.deals table.

create extension if not exists pgcrypto;

create table if not exists public.extracted_deals (
    id                 uuid primary key default gen_random_uuid(),
    raw_source_id      uuid references public.raw_sources(id) on delete cascade,
    source_url         text,
    company_name       text,
    country            text,
    amount_usd         bigint,
    amount_original    text,
    currency_original  text,
    stage              text,
    announcement_date  text,
    sector             text,
    sub_sector         text,
    lead_investor      text,
    co_investors       jsonb not null default '[]'::jsonb,
    website            text,
    is_funding_round   boolean,
    confidence_score   numeric,
    extraction_notes   text,
    status             text not null default 'needs_review',
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

alter table public.extracted_deals
    add column if not exists raw_source_id uuid
        references public.raw_sources(id) on delete cascade,
    add column if not exists source_url text,
    add column if not exists company_name text,
    add column if not exists country text,
    add column if not exists amount_usd bigint,
    add column if not exists amount_original text,
    add column if not exists currency_original text,
    add column if not exists stage text,
    add column if not exists announcement_date text,
    add column if not exists sector text,
    add column if not exists sub_sector text,
    add column if not exists lead_investor text,
    add column if not exists co_investors jsonb not null default '[]'::jsonb,
    add column if not exists website text,
    add column if not exists is_funding_round boolean,
    add column if not exists confidence_score numeric,
    add column if not exists extraction_notes text,
    add column if not exists extraction_payload jsonb,
    add column if not exists extraction_status text not null default 'pending',
    add column if not exists status text not null default 'needs_review';

do $$
begin
    if exists (
        select 1
        from information_schema.columns
        where table_schema = 'public'
          and table_name = 'extracted_deals'
          and column_name = 'co_investors'
          and data_type <> 'jsonb'
    ) then
        alter table public.extracted_deals
            alter column co_investors type jsonb
            using case
                when co_investors is null or btrim(co_investors) = ''
                    then '[]'::jsonb
                else to_jsonb(string_to_array(co_investors, ', '))
            end;
    end if;
end$$;

alter table public.extracted_deals
    alter column co_investors set default '[]'::jsonb;

update public.extracted_deals
set co_investors = '[]'::jsonb
where co_investors is null;

alter table public.extracted_deals
    alter column co_investors set not null;

alter table public.extracted_deals
    drop constraint if exists extracted_deals_status_chk;

alter table public.extracted_deals
    add constraint extracted_deals_status_chk
        check (
            extraction_status in (
                'pending',
                'needs_review',
                'approved',
                'extracted',
                'not_a_funding_round',
                'reviewed',
                'rejected',
                'extraction_failed'
            )
        );

alter table public.extracted_deals
    drop constraint if exists extracted_deals_review_status_chk;

alter table public.extracted_deals
    add constraint extracted_deals_review_status_chk
        check (status in ('needs_review', 'approved', 'rejected'));

create index if not exists extracted_deals_status_idx
    on public.extracted_deals (status);
