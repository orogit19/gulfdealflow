-- Migration 005 - VC portfolio ingestion MVP tables.
--
-- Portfolio pages create investor-company relationship drafts only. They do
-- not create funding-round records.

create extension if not exists pgcrypto;

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

create table if not exists public.ingestion_logs (
    id              uuid primary key default gen_random_uuid(),
    source_page_id  uuid references public.source_pages(id) on delete set null,
    url             text,
    event           text not null,
    status          text not null,
    message         text,
    metadata        jsonb,
    created_at      timestamptz not null default now()
);

alter table public.ingestion_logs
    add column if not exists source_page_id uuid references public.source_pages(id)
        on delete set null;

create index if not exists ingestion_logs_source_page_id_idx
    on public.ingestion_logs (source_page_id);

create or replace function public.set_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

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

alter table public.investors enable row level security;
alter table public.portfolio_companies enable row level security;
alter table public.source_pages enable row level security;
alter table public.investor_company_relationships enable row level security;
alter table public.ingestion_logs enable row level security;
