-- Manual-adjust backend: a private key/value table holding the short-lived MMS
-- token that the login feeder mints. It is a SECRET (a live admin session token),
-- so it must NOT be readable by anon. RLS is enabled with NO anon policy, so only
-- the service_role (which bypasses RLS) — i.e. the feeder and the Edge Function —
-- can read/write it. The public dashboard NEVER reads this table; it only calls
-- the mms-adjust Edge Function, which reads the token server-side.

create table if not exists app_settings (
  key        text primary key,
  value      text,
  updated_at timestamptz not null default now()
);

alter table app_settings enable row level security;
-- (intentionally no policy for anon/authenticated → only service_role can touch it)
