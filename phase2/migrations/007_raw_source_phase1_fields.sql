-- Migration 007 - Explicit Phase 1 raw source fields.
--
-- Adds the field names used by the URL ingestion MVP while preserving the
-- earlier extracted_text/source_type columns used elsewhere in the backend.

alter table public.raw_sources
    add column if not exists source_name text,
    add column if not exists domain text,
    add column if not exists raw_text text;

create index if not exists raw_sources_domain_idx
    on public.raw_sources (domain);
