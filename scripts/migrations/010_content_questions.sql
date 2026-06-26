-- =============================================================================
-- Migration 010: content schema + content.questions (Content-Ops Phase 1)
-- Date:     2026-06-26
-- Author:   Edison (Analytics Engineer)
-- Purpose:  Stand up the content-operations data layer Phase 1 per
--           "Content-Operations Data Layer — Proposal (Edison)" §A.
--           Loads the Daggerheart Question Bank (Master) into a queryable,
--           upsertable table so dashboards/Carson can query the bank and
--           future content↔question linkage has a foundation.
--
-- Scope (ADDITIVE ONLY — nothing existing is touched/altered/dropped):
--   * new schema  `content`
--   * new enums   `content.recurrence`, `content.tie_in`
--   * new table   `content.questions` + indexes
--
-- Idempotent: safe to re-run (create-if-not-exists; enum/table guarded).
-- =============================================================================

create schema if not exists content;

-- Enums (guarded so re-run is safe; CREATE TYPE has no IF NOT EXISTS) -----------
do $$
begin
  if not exists (
    select 1 from pg_type t
    join pg_namespace n on n.oid = t.typnamespace
    where t.typname = 'recurrence' and n.nspname = 'content'
  ) then
    create type content.recurrence as enum ('common','occasional','rare');
  end if;
end$$;

do $$
begin
  if not exists (
    select 1 from pg_type t
    join pg_namespace n on n.oid = t.typnamespace
    where t.typname = 'tie_in' and n.nspname = 'content'
  ) then
    create type content.tie_in as enum ('Y','maybe','N');
  end if;
end$$;

-- Table ------------------------------------------------------------------------
create table if not exists content.questions (
  question_id    bigint generated always as identity primary key,
  question_hash  text not null unique,            -- sha256(normalized question text) = dedup/upsert key
  question_text  text not null,
  category       text not null,                   -- from the ### header (N stripped)
  sub_topic      text,
  recurrence     content.recurrence not null,
  product_tie_in content.tie_in not null default 'N',
  tie_in_note    text,                            -- shortlist "Product / demo" prose (optional)
  source_url     text,                            -- normalized permalink
  source_batch   text,                            -- 'master-2026-06-23', 'f5bot-2026-07', ...
  first_seen_at  timestamptz not null default now(),
  last_seen_at   timestamptz not null default now(),
  is_active      boolean not null default true,   -- soft-retire (mirrors core.parts)
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

create index if not exists ix_content_questions_category   on content.questions (category);
create index if not exists ix_content_questions_recurrence  on content.questions (recurrence);
create index if not exists ix_content_questions_tie_in      on content.questions (product_tie_in);
create index if not exists ix_content_questions_active      on content.questions (is_active);
create index if not exists ix_content_questions_batch       on content.questions (source_batch);
