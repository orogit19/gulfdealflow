-- Migration 009 - Review audit metadata for extracted deal drafts.
--
-- Records when a draft was reviewed and which canonical deal row was created
-- or matched on approval.

alter table public.extracted_deals
    add column if not exists reviewed_at timestamptz,
    add column if not exists approved_deal_id text references public.deals(deal_id) on delete set null;

create index if not exists extracted_deals_reviewed_at_idx
    on public.extracted_deals (reviewed_at desc);

create index if not exists extracted_deals_approved_deal_id_idx
    on public.extracted_deals (approved_deal_id);
