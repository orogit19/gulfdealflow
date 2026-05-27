-- GulfDealFlow: deals table
-- Run this in Supabase SQL Editor (Project → SQL Editor → New query → paste → Run).

create table if not exists public.deals (
    deal_id         text primary key,
    company_name    text,
    country         text,
    city            text,
    date            text,
    stage           text,
    amount_usd      bigint,
    disclosed       boolean,
    sector          text,
    description     text,
    founded_year    text,
    website         text,
    lead_investor   text,
    co_investors    text,
    investor_types  text,
    source          text,
    notes           text
);

-- Filter indexes for the API's common query paths.
create index if not exists deals_country_idx on public.deals (country);
create index if not exists deals_sector_idx  on public.deals (sector);
create index if not exists deals_stage_idx   on public.deals (stage);
create index if not exists deals_date_idx    on public.deals (date);

-- A company can have multiple rounds at the same stage (e.g. Seed +
-- Seed extension years apart) — so the uniqueness key is the triple
-- (company_name, stage, date). The loader uses this same triple as the
-- upsert conflict target so a CSV reload updates the matching row instead
-- of inserting a duplicate. NULL dates are treated as distinct by default,
-- which is acceptable while only a handful of rows have undisclosed dates.
do $$
begin
    -- Drop any earlier (company_name, stage) constraint from migration 001.
    alter table public.deals
        drop constraint if exists deals_company_stage_uniq;

    if not exists (
        select 1 from pg_constraint where conname = 'deals_company_stage_date_uniq'
    ) then
        alter table public.deals
            add constraint deals_company_stage_date_uniq
            unique (company_name, stage, date);
    end if;
end$$;

-- Row-level security: enable RLS and allow the anon role to read.
-- The service role bypasses RLS, so the loader still works.
alter table public.deals enable row level security;

drop policy if exists "deals_anon_read" on public.deals;
create policy "deals_anon_read"
    on public.deals
    for select
    to anon
    using (true);


-- Phase 1 article ingestion tables.
-- API writes use SUPABASE_SERVICE_ROLE_KEY, which bypasses RLS. No public
-- insert/update policies are created here.
create extension if not exists pgcrypto;

create table if not exists public.raw_sources (
    id              uuid primary key default gen_random_uuid(),
    url             text not null unique,
    source_type     text not null default 'url',
    source_name     text,
    domain          text,
    title           text,
    raw_text        text,
    extracted_text  text,
    status          text not null default 'pending',
    error_message   text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    constraint raw_sources_status_chk
        check (
            status in (
                'pending',
                'fetched',
                'fetch_failed',
                'extraction_failed',
                'extracted',
                'not_a_funding_round'
            )
        )
);

create table if not exists public.extracted_deals (
    id                 uuid primary key default gen_random_uuid(),
    raw_source_id      uuid references public.raw_sources(id) on delete cascade,
    investor_company_relationship_id uuid,
    source_url         text,
    company_name       text,
    country            text,
    city               text,
    stage              text,
    amount_usd         bigint,
    amount_original    text,
    currency_original  text,
    announced_date     text,
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
    extraction_status  text not null default 'pending',
    extraction_payload  jsonb,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    constraint extracted_deals_status_chk
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
        ),
    constraint extracted_deals_review_status_chk
        check (status in ('needs_review', 'approved', 'rejected'))
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

create table if not exists public.investors (
    id          uuid primary key default gen_random_uuid(),
    name        text not null unique,
    website     text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create table if not exists public.portfolio_companies (
    id          uuid primary key default gen_random_uuid(),
    name        text not null unique,
    website     text,
    sector      text,
    geography   text,
    country     text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create table if not exists public.source_pages (
    id             uuid primary key default gen_random_uuid(),
    investor_id    uuid references public.investors(id) on delete set null,
    investor_name  text not null,
    url            text not null,
    title          text,
    visible_text   text,
    links          jsonb,
    status         text not null default 'fetched',
    error_message  text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (investor_name, url),
    constraint source_pages_status_chk
        check (status in ('pending', 'fetched', 'failed'))
);

create table if not exists public.investor_company_relationships (
    id                 uuid primary key default gen_random_uuid(),
    investor_id        uuid references public.investors(id) on delete set null,
    company_id         uuid references public.portfolio_companies(id) on delete set null,
    source_page_id     uuid references public.source_pages(id) on delete set null,
    investor_name      text not null,
    company_name       text not null,
    company_website    text,
    sector             text,
    geography          text,
    country            text,
    source_url         text not null,
    extraction_method  text,
    confidence_score   numeric,
    status             text not null default 'needs_review',
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    constraint investor_company_relationships_status_chk
        check (status in ('needs_review', 'approved', 'rejected'))
);

create index if not exists raw_sources_status_idx
    on public.raw_sources (status);
create index if not exists raw_sources_created_at_idx
    on public.raw_sources (created_at desc);
create index if not exists raw_sources_domain_idx
    on public.raw_sources (domain);
create index if not exists extracted_deals_raw_source_id_idx
    on public.extracted_deals (raw_source_id);
create index if not exists extracted_deals_relationship_id_idx
    on public.extracted_deals (investor_company_relationship_id);
create index if not exists extracted_deals_extraction_status_idx
    on public.extracted_deals (extraction_status);
create index if not exists extracted_deals_status_idx
    on public.extracted_deals (status);
create index if not exists ingestion_logs_raw_source_id_idx
    on public.ingestion_logs (raw_source_id);
create index if not exists ingestion_logs_created_at_idx
    on public.ingestion_logs (created_at desc);
create unique index if not exists investor_company_relationships_name_uniq
    on public.investor_company_relationships (
        lower(investor_name),
        lower(company_name)
    );
create index if not exists investors_name_idx
    on public.investors (name);
create index if not exists portfolio_companies_name_idx
    on public.portfolio_companies (name);
create index if not exists source_pages_investor_name_idx
    on public.source_pages (investor_name);
create index if not exists investor_company_relationships_status_idx
    on public.investor_company_relationships (status);

alter table public.ingestion_logs
    add column if not exists source_page_id uuid references public.source_pages(id)
        on delete set null;

create index if not exists ingestion_logs_source_page_id_idx
    on public.ingestion_logs (source_page_id);

do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conname = 'extracted_deals_relationship_id_fkey'
    ) then
        alter table public.extracted_deals
            add constraint extracted_deals_relationship_id_fkey
            foreign key (investor_company_relationship_id)
            references public.investor_company_relationships(id)
            on delete set null;
    end if;
end$$;

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

drop trigger if exists investors_set_updated_at on public.investors;
create trigger investors_set_updated_at
    before update on public.investors
    for each row execute function public.set_updated_at();

drop trigger if exists portfolio_companies_set_updated_at
    on public.portfolio_companies;
create trigger portfolio_companies_set_updated_at
    before update on public.portfolio_companies
    for each row execute function public.set_updated_at();

drop trigger if exists source_pages_set_updated_at on public.source_pages;
create trigger source_pages_set_updated_at
    before update on public.source_pages
    for each row execute function public.set_updated_at();

drop trigger if exists investor_company_relationships_set_updated_at
    on public.investor_company_relationships;
create trigger investor_company_relationships_set_updated_at
    before update on public.investor_company_relationships
    for each row execute function public.set_updated_at();

alter table public.raw_sources enable row level security;
alter table public.extracted_deals enable row level security;
alter table public.ingestion_logs enable row level security;
alter table public.investors enable row level security;
alter table public.portfolio_companies enable row level security;
alter table public.source_pages enable row level security;
alter table public.investor_company_relationships enable row level security;
