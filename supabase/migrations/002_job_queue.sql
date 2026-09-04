-- Run once in the Supabase SQL editor, after 001_usage.sql.
-- Adds a durable job queue so generation happens outside Vercel's 300s limit.

create table if not exists public.generation_jobs (
  id uuid primary key,                 -- same id as public.generations.id
  user_id uuid not null,
  prompt text not null,
  status text not null default 'queued',
  attempts integer not null default 0,
  model text,
  result_html text,
  result_summary text,
  error_message text,
  created_at timestamptz not null default now(),
  claimed_at timestamptz,
  finished_at timestamptz,
  generation_time_ms integer
);

alter table public.generation_jobs
  drop constraint if exists generation_jobs_status_check,
  add constraint generation_jobs_status_check
    check (status in ('queued', 'running', 'completed', 'failed'));

create index if not exists generation_jobs_status_created_idx
  on public.generation_jobs (status, created_at);

-- The queue is only ever touched by the Vercel API and the GitHub Actions
-- worker, both of which use the service role key. No anon/authenticated grants.
alter table public.generation_jobs enable row level security;
grant all on public.generation_jobs to service_role;

-- Atomically hand exactly one job to a worker.
-- Also re-claims jobs that a worker started but never finished (crash, cancelled
-- run, runner timeout) after `p_stale_seconds`.
create or replace function public.claim_generation_job(p_stale_seconds integer default 900)
returns table (
  id uuid,
  user_id uuid,
  prompt text,
  attempts integer
)
language plpgsql
security definer
set search_path = public
as $$
declare
  job_id uuid;
begin
  select j.id into job_id
  from public.generation_jobs j
  where j.status = 'queued'
     or (j.status = 'running'
         and j.claimed_at < now() - make_interval(secs => p_stale_seconds))
  order by j.created_at
  for update skip locked
  limit 1;

  if job_id is null then
    return;
  end if;

  update public.generation_jobs
  set status = 'running',
      claimed_at = now(),
      attempts = generation_jobs.attempts + 1,
      error_message = null
  where generation_jobs.id = job_id;

  return query
  select j.id, j.user_id, j.prompt, j.attempts
  from public.generation_jobs j
  where j.id = job_id;
end;
$$;

-- Store the finished document and mirror the outcome onto public.generations
-- so the existing daily-quota logic keeps working unchanged.
create or replace function public.finish_generation_job(
  p_job_id uuid,
  p_status text,
  p_html text,
  p_summary text,
  p_error text,
  p_model text,
  p_generation_time_ms integer,
  p_max_attempts integer default 3
)
returns text
language plpgsql
security definer
set search_path = public
as $$
declare
  current_attempts integer;
  final_status text;
begin
  if p_status not in ('completed', 'failed') then
    raise exception 'invalid job status';
  end if;

  select attempts into current_attempts
  from public.generation_jobs
  where id = p_job_id;

  if current_attempts is null then
    return 'missing';
  end if;

  -- A failure that still has retries left goes back on the queue.
  if p_status = 'failed' and current_attempts < p_max_attempts then
    final_status := 'queued';
  else
    final_status := p_status;
  end if;

  update public.generation_jobs
  set status = final_status,
      result_html = coalesce(p_html, result_html),
      result_summary = coalesce(p_summary, result_summary),
      error_message = p_error,
      model = coalesce(p_model, model),
      generation_time_ms = coalesce(p_generation_time_ms, generation_time_ms),
      finished_at = case when final_status in ('completed', 'failed') then now() else null end,
      claimed_at = case when final_status = 'queued' then null else claimed_at end
  where id = p_job_id;

  if final_status in ('completed', 'failed') then
    update public.generations
    set status = final_status,
        generation_time_ms = coalesce(p_generation_time_ms, generation_time_ms)
    where id = p_job_id;
  end if;

  return final_status;
end;
$$;

revoke all on function public.claim_generation_job(integer) from public;
revoke all on function public.finish_generation_job(uuid, text, text, text, text, text, integer, integer) from public;
grant execute on function public.claim_generation_job(integer) to service_role;
grant execute on function public.finish_generation_job(uuid, text, text, text, text, text, integer, integer) to service_role;

-- Housekeeping: finished documents are large, so drop them after a day.
create or replace function public.purge_old_generation_jobs(p_older_than_hours integer default 24)
returns integer
language sql
security definer
set search_path = public
as $$
  with cleared as (
    update public.generation_jobs
    set result_html = null
    where result_html is not null
      and finished_at < now() - make_interval(hours => p_older_than_hours)
    returning 1
  )
  select count(*)::integer from cleared;
$$;

grant execute on function public.purge_old_generation_jobs(integer) to service_role;
