-- Run this once in Supabase SQL Editor before deploying the application.
-- The app supplies the daily-reset timezone. Its default is Asia/Colombo.

alter table public.generations
  add column if not exists event_type text not null default 'visualization';

alter table public.generations
  drop constraint if exists generations_status_check,
  add constraint generations_status_check
    check (status in ('pending', 'completed', 'failed'));

alter table public.generations
  drop constraint if exists generations_event_type_check,
  add constraint generations_event_type_check
    check (event_type in ('visualization', 'follow_up'));

create index if not exists generations_user_type_created_idx
  on public.generations (user_id, event_type, created_at);

-- `last_seen` changes on every request while `first_seen` remains immutable.
create or replace function public.set_user_last_seen()
returns trigger
language plpgsql
as $$
begin
  new.last_seen = now();
  return new;
end;
$$;

drop trigger if exists users_set_last_seen on public.users;
create trigger users_set_last_seen
before update on public.users
for each row execute function public.set_user_last_seen();

-- Reserve usage and create the pending audit record in one transaction.
-- Only completed visualizations count toward the daily quota. Pending rows are
-- tracked for status flow, but they are not included in the cap calculation.
create or replace function public.start_usage_event(
  p_user_id uuid,
  p_event_type text,
  p_prompt text,
  p_ip_address text,
  p_daily_limit integer,
  p_timezone text
)
returns table (allowed boolean, generation_id uuid)
language plpgsql
security definer
set search_path = public
as $$
declare
  day_start timestamptz := date_trunc('day', now() at time zone 'UTC');
  used_count integer;
  new_id uuid;
begin
  if p_event_type not in ('visualization', 'follow_up') or p_daily_limit < 1 then
    raise exception 'invalid usage event';
  end if;

  perform pg_advisory_xact_lock(hashtextextended(
    p_user_id::text || ':' || p_event_type || ':' || day_start::date::text, 0
  ));

  insert into public.users (user_id)
  values (p_user_id)
  on conflict (user_id) do update set last_seen = now();

  select count(*) into used_count
  from public.generations
  where user_id = p_user_id
    and event_type = p_event_type
    and created_at >= day_start
    and status = 'completed';

  if used_count >= p_daily_limit then
    return query select false, null::uuid;
    return;
  end if;

  new_id := gen_random_uuid();
  insert into public.generations (
    id, user_id, event_type, status, prompt, ip_address
  ) values (
    new_id, p_user_id, p_event_type, 'pending', p_prompt, p_ip_address
  );

  return query select true, new_id;
end;
$$;

create or replace view public.daily_usage
with (security_invoker = on)
as
select
  (created_at at time zone 'UTC')::date as usage_date,
  user_id,
  event_type,
  count(*) filter (where status = 'completed') as successful_requests,
  count(*) filter (where status = 'failed') as failed_requests,
  count(*) filter (where status = 'pending') as pending_requests
from public.generations
group by 1, 2, 3;

revoke all on function public.start_usage_event(uuid, text, text, text, integer, text) from public;
grant execute on function public.start_usage_event(uuid, text, text, text, integer, text) to service_role;
