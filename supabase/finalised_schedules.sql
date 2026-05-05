-- Finalised schedule document storage for the Scheduler app.
-- Run this in the Supabase SQL editor after supabase/schema.sql.
--
-- Backend endpoint:
--   POST /api/finalise-schedule
--
-- Required backend environment variables:
--   SUPABASE_URL=https://<project-ref>.supabase.co
--   SUPABASE_SERVICE_ROLE_KEY=<service-role-key>

create extension if not exists pgcrypto;

create table if not exists public.finalised_schedules (
  id uuid primary key default gen_random_uuid(),
  week_start date not null unique,
  week_end date not null,
  status text not null default 'finalised',
  file_name text not null,
  mime_type text not null default 'application/pdf',
  file_base64 text not null,
  schedule_data jsonb not null default '{}'::jsonb,
  summary jsonb not null default '{}'::jsonb,
  finalised_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists finalised_schedules_week_start_idx
  on public.finalised_schedules (week_start desc);

create index if not exists finalised_schedules_summary_gin_idx
  on public.finalised_schedules using gin (summary);

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists finalised_schedules_touch_updated_at on public.finalised_schedules;
create trigger finalised_schedules_touch_updated_at
before update on public.finalised_schedules
for each row
execute function public.touch_updated_at();

alter table public.finalised_schedules enable row level security;

-- Backend uses SUPABASE_SERVICE_ROLE_KEY, which bypasses RLS.
-- These policies are only for authenticated admin clients if you later expose
-- this table in a Supabase dashboard.
drop policy if exists "finalised_schedules_authenticated_read" on public.finalised_schedules;
create policy "finalised_schedules_authenticated_read"
on public.finalised_schedules
for select
to authenticated
using (true);

drop policy if exists "finalised_schedules_authenticated_insert" on public.finalised_schedules;
create policy "finalised_schedules_authenticated_insert"
on public.finalised_schedules
for insert
to authenticated
with check (true);

drop policy if exists "finalised_schedules_authenticated_update" on public.finalised_schedules;
create policy "finalised_schedules_authenticated_update"
on public.finalised_schedules
for update
to authenticated
using (true)
with check (true);
