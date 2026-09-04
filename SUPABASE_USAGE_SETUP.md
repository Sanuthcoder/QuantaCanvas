# Supabase usage setup

Run [supabase/migrations/001_usage.sql](supabase/migrations/001_usage.sql) once in the Supabase SQL Editor. It adds the `event_type` field needed to distinguish visualization from follow-up chat records and installs the atomic quota function.

Set these server-side Vercel environment variables:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `DAILY_GENERATION_LIMIT=7` (optional; 7 is the default)
- `DAILY_CHAT_GENERATION_LIMIT=50` (optional; 50 is the default)
- `USAGE_TIMEZONE=Asia/Colombo` (optional; this is the default)

The reset is at **00:00 in `USAGE_TIMEZONE`**. To find daily successful visualizations:

```sql
select usage_date, sum(successful_requests) as successful_visualizations
from public.daily_usage
where event_type = 'visualization'
group by usage_date
order by usage_date desc;
```

`daily_usage` uses the default `Asia/Colombo` date. If you set a different `USAGE_TIMEZONE`, use that timezone in a direct `generations` query for per-day reporting.

The browser stores a random UUID to identify an unauthenticated visitor. This is suitable for a courtesy quota, not an abuse-proof identity system: a visitor can clear storage or supply another UUID. For strict per-person limits, replace that ID with a verified Supabase Auth user ID.

Prompts and IP addresses are personal data. Retain them only as long as necessary and disclose this collection in your privacy policy.
