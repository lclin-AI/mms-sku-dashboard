-- Auto add-back inventory.
--
-- Per SKU the user sets a target delivery date D and an expected selling qty Q.
-- On enable, MMS inventory is set to Q. Then every 5-min sync computes how much
-- MORE has sold on delivery dates AFTER D since the previous sync (the
-- "incremental"), and — if enabled and positive — adds that back to MMS (capped
-- at Q), so orders for later dates don't eat into D's reserved Q. Negative
-- incrementals (refunds/cancels) are ignored.

-- 1) config: what the user set. Written only by the mms-adjust Edge Function
-- (password-gated); anon may READ so the dashboard can show current settings.
create table if not exists autoback_config (
  store_code    text        not null,
  sku_id        text        not null,
  enabled       boolean     not null default false,
  delivery_date date        not null,
  expected_qty  numeric     not null,
  updated_at    timestamptz not null default now(),
  primary key (store_code, sku_id)
);
alter table autoback_config enable row level security;
drop policy if exists autoback_config_anon_read on autoback_config;
create policy autoback_config_anon_read on autoback_config
  for select to anon, authenticated using (true);

-- 2) state: the running snapshot the sync job keeps to derive the incremental,
-- plus the last incremental (the "verify" figure the dashboard shows even when
-- the SKU is not enabled). Service-role writes; anon reads for the verify field.
create table if not exists autoback_state (
  store_code       text        not null,
  sku_id           text        not null,
  sold_after_d     numeric,               -- Σ sold on dates > D at last sync
  last_incremental numeric,               -- most recent (now - previous)
  last_added       numeric,               -- what was actually added (after cap)
  last_run_at      timestamptz,
  primary key (store_code, sku_id)
);
alter table autoback_state enable row level security;
drop policy if exists autoback_state_anon_read on autoback_state;
create policy autoback_state_anon_read on autoback_state
  for select to anon, authenticated using (true);
