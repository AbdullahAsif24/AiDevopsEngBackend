-- AI DevOps Engineer — Supabase schema (DevOps / Infra)
-- Run this in the Supabase SQL editor once per project.
-- v1 keeps it minimal: jobs + deployments + logs.

create table if not exists public.jobs (
  id text primary key,
  repo_url text not null,
  status text not null,
  dockerfile_content text,
  deploy_url text,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.deployments (
  id uuid primary key default gen_random_uuid(),
  job_id text references public.jobs (id) on delete set null,
  provider text not null default 'render',
  service_id text,
  live_url text,
  image_tag text,
  status text not null,
  is_active boolean not null default false,
  created_at timestamptz not null default now()
);

create index if not exists deployments_job_id_idx on public.deployments (job_id);
create index if not exists deployments_active_idx on public.deployments (is_active);

create table if not exists public.logs (
  id bigserial primary key,
  job_id text,
  stage text,
  message text,
  created_at timestamptz not null default now()
);

create index if not exists logs_job_id_idx on public.logs (job_id);

-- Service role key bypasses RLS; still enable RLS and deny anon for safety.
alter table public.jobs enable row level security;
alter table public.deployments enable row level security;
alter table public.logs enable row level security;
