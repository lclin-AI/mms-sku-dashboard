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


-- Manual-adjust history log. One row per successful inventory write, inserted by
-- the mms-adjust Edge Function (service_role). Anon may READ it so the dashboard
-- can show the history; only service_role INSERTs (no anon write policy).
create table if not exists adjust_log (
  id         bigint generated always as identity primary key,
  ts         timestamptz not null default now(),
  store_code text        not null,
  sku_id     text        not null,
  sku_name   text,
  mode       text        not null,     -- add | deduct | set
  qty        numeric     not null,
  before_qty numeric,
  after_qty  numeric,
  warehouse  text,
  operator   text,
  status     text        not null default 'success',   -- success | fail
  error      text
);
create index if not exists adjust_log_ts_idx on adjust_log (ts desc);
create index if not exists adjust_log_sku_idx on adjust_log (store_code, sku_id, ts desc);

alter table adjust_log enable row level security;
drop policy if exists adjust_log_anon_read on adjust_log;
create policy adjust_log_anon_read on adjust_log for select to anon, authenticated using (true);
