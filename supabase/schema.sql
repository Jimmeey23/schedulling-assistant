-- Physique 57 Scheduler Supabase schema
-- Run this in the Supabase SQL editor.
--
-- Backend environment variables expected by the app:
--   SUPABASE_URL=https://<project-ref>.supabase.co
--   SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
--
-- The browser does not need Supabase credentials. All reads/writes go through
-- backend endpoints.

create extension if not exists pgcrypto;

create table if not exists public.studio_rules (
  id uuid primary key default gen_random_uuid(),
  config_key text not null unique,
  data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists studio_rules_config_key_idx
  on public.studio_rules (config_key);

create index if not exists studio_rules_data_gin_idx
  on public.studio_rules using gin (data);

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists studio_rules_touch_updated_at on public.studio_rules;
create trigger studio_rules_touch_updated_at
before update on public.studio_rules
for each row
execute function public.touch_updated_at();

alter table public.studio_rules enable row level security;

-- Service-role requests bypass RLS. These policies also allow backend clients
-- configured with SUPABASE_ANON_KEY to read/write through the server. For
-- production, prefer SUPABASE_SERVICE_ROLE_KEY on the backend only.
drop policy if exists "studio_rules_backend_read" on public.studio_rules;
create policy "studio_rules_backend_read"
on public.studio_rules
for select
to anon, authenticated
using (true);

drop policy if exists "studio_rules_backend_insert" on public.studio_rules;
create policy "studio_rules_backend_insert"
on public.studio_rules
for insert
to anon, authenticated
with check (true);

drop policy if exists "studio_rules_backend_update" on public.studio_rules;
create policy "studio_rules_backend_update"
on public.studio_rules
for update
to anon, authenticated
using (true)
with check (true);

-- Optional seed rows. The app will upsert these same config keys.
insert into public.studio_rules (config_key, data)
values
  ('schedule_config', '{}'::jsonb),
  ('trainer_profiles', '[]'::jsonb),
  ('rules_catalog', '{}'::jsonb),
  ('rules_kwality', '{}'::jsonb),
  ('rules_supreme', '{}'::jsonb),
  ('rules_kenkere', '{}'::jsonb)
on conflict (config_key) do nothing;
