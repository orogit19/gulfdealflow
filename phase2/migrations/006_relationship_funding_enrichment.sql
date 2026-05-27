-- Migration 006 - Link funding extractions to approved portfolio relationships.
--
-- Enrichment writes extracted_deals as review drafts. Approval can later move
-- records into the canonical deals table.

alter table public.extracted_deals
    add column if not exists investor_company_relationship_id uuid
        references public.investor_company_relationships(id) on delete set null,
    add column if not exists source_url text;

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

create index if not exists extracted_deals_relationship_id_idx
    on public.extracted_deals (investor_company_relationship_id);
create index if not exists extracted_deals_extraction_status_idx
    on public.extracted_deals (extraction_status);
