-- Migration 003 - Phase 1 article ingestion tables.
--
-- Run this in the Supabase SQL Editor. Idempotent: tables, indexes, and
-- trigger/function definitions can be safely re-run.

create extension if not exists pgcrypto;

create table if not exists public.raw_sources (
    id              uuid primary key default gen_random_uuid(),
    url             text not null unique,
    source_type     text not null default 'url',
    title           text,
    extracted_text  text,
    status          text not null default 'pending',
    error_message   text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    constraint raw_sources_status_chk
        check (status in ('pending', 'fetched', 'fetch_failed', 'extraction_failed'))
);

create table if not exists public.extracted_deals (
    id                 uuid primary key default gen_random_uuid(),
    raw_source_id      uuid references public.raw_sources(id) on delete cascade,
    company_name       text,
    country            text,
    city               text,
    stage              text,
    amount_usd         bigint,
    announced_date     text,
    lead_investor      text,
    co_investors       text,
    extraction_status  text not null default 'pending',
    extraction_payload  jsonb,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    constraint extracted_deals_status_chk
        check (extraction_status in ('pending', 'extracted', 'reviewed', 'rejected'))
);

create table if not exists public.ingestion_logs (
    id             uuid primary key default gen_random_uuid(),
    raw_source_id  uuid references public.raw_sources(id) on delete set null,
    url            text,
    event          text not null,
    status         text not null,
    message        text,
    metadata       jsonb,
    created_at     timestamptz not null default now()
);

create index if not exists raw_sources_status_idx
    on public.raw_sources (status);
create index if not exists raw_sources_created_at_idx
    on public.raw_sources (created_at desc);
create index if not exists extracted_deals_raw_source_id_idx
    on public.extracted_deals (raw_source_id);
create index if not exists ingestion_logs_raw_source_id_idx
    on public.ingestion_logs (raw_source_id);
create index if not exists ingestion_logs_created_at_idx
    on public.ingestion_logs (created_at desc);

create or replace function public.set_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists raw_sources_set_updated_at on public.raw_sources;
create trigger raw_sources_set_updated_at
    before update on public.raw_sources
    for each row execute function public.set_updated_at();

drop trigger if exists extracted_deals_set_updated_at on public.extracted_deals;
create trigger extracted_deals_set_updated_at
    before update on public.extracted_deals
    for each row execute function public.set_updated_at();

alter table public.raw_sources enable row level security;
alter table public.extracted_deals enable row level security;
alter table public.ingestion_logs enable row level security;
