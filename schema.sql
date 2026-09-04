-- MMS SKU sales dashboard: one table, finest grain we need.
-- Aggregated at (store, order_date, delivery_date, sku) so BOTH an order-date
-- view and a delivery-date view are derivable, and a re-pull of one order_date
-- can safely replace exactly that day's rows.

create table if not exists mms_sku_daily (
  store_code    text        not null,
  order_date    date        not null,
  delivery_date date        not null,
  sku_id        text        not null,
  sku_name_zh   text,
  sku_name_en   text,
  qty           numeric     not null default 0,
  amount        numeric     not null default 0,
  lines         integer     not null default 0,
  updated_at    timestamptz not null default now(),
  primary key (store_code, order_date, delivery_date, sku_id)
);

create index if not exists mms_sku_daily_sku_idx
  on mms_sku_daily (store_code, sku_id);
create index if not exists mms_sku_daily_delivery_idx
  on mms_sku_daily (store_code, delivery_date);

-- A tiny lookup the dashboard uses to fill the SKU dropdown without pulling
-- every fact row.
create or replace view mms_sku_catalog as
  select store_code,
         sku_id,
         max(sku_name_zh)            as sku_name_zh,
         max(sku_name_en)            as sku_name_en,
         sum(qty)                    as qty_total,
         sum(amount)                 as amount_total,
         min(order_date)             as first_order_date,
         max(order_date)             as last_order_date
  from mms_sku_daily
  group by store_code, sku_id;

alter table mms_sku_daily enable row level security;

-- Public dashboard: read-only for anon. Writes are service_role only
-- (service_role bypasses RLS, so it needs no policy).
drop policy if exists mms_sku_daily_anon_read on mms_sku_daily;
create policy mms_sku_daily_anon_read
  on mms_sku_daily for select
  to anon, authenticated
  using (true);

-- Per-date aggregate for the "all SKU" overview. The raw table has ~12k rows,
-- and PostgREST caps a response at ~1000, so the overview reads this small view
-- (store x order_date x delivery_date) and re-groups by either date client-side.
create or replace view mms_date_grain as
  select store_code, order_date, delivery_date,
         sum(qty) as qty, sum(amount) as amount, sum(lines) as lines
  from mms_sku_daily
  group by store_code, order_date, delivery_date;

-- iMAX PO/GR (and later disposal) per SKU per booking date. Populated by a LOCAL
-- job (imax_to_supabase.py) — iMAX is behind a WAF and internal-network only, so
-- this cannot run in the cloud.
create table if not exists imax_daily (
  store_code text not null, date date not null, sku_id text not null,
  po_qty numeric not null default 0, gr_qty numeric not null default 0,
  disposal_qty numeric, updated_at timestamptz not null default now(),
  primary key (store_code, date, sku_id));
alter table imax_daily enable row level security;
create policy imax_daily_anon_read on imax_daily for select to anon, authenticated using (true);

-- Daily 6PM physical-stock snapshot (excl R/X locations). Populated by
-- imax_stock6pm_to_supabase.py at ~18:00. "Remaining" for date D on the tracker
-- = this snapshot from D-1. Forward-only (cannot be backfilled).
create table if not exists imax_stock_6pm (
  store_code text not null, date date not null, sku_id text not null,
  phys_qty numeric not null default 0, updated_at timestamptz not null default now(),
  primary key (store_code, date, sku_id));
alter table imax_stock_6pm enable row level security;
create policy imax_stock_6pm_anon_read on imax_stock_6pm for select to anon, authenticated using (true);

-- IIMS stock-levels reference. imax_iims_sku = one row per SKU (total quantity +
-- has_bydate). imax_iims_bydate = per-date rows when IIMS returns dateInventory[].
-- Populated by imax_iims_to_supabase.py (local, ~5 min); IIMS s2s is not
-- browser/CORS reachable so it cannot be called live from the page.
create table if not exists imax_iims_sku (
  store_code text not null, sku_id text not null, quantity numeric,
  has_bydate boolean not null default false, update_stock_time text,
  updated_at timestamptz not null default now(), primary key (store_code, sku_id));
create table if not exists imax_iims_bydate (
  store_code text not null, sku_id text not null, date date not null,
  quantity numeric not null default 0, updated_at timestamptz not null default now(),
  primary key (store_code, sku_id, date));
alter table imax_iims_sku enable row level security;
alter table imax_iims_bydate enable row level security;
create policy p2 on imax_iims_sku for select to anon, authenticated using (true);
create policy p1 on imax_iims_bydate for select to anon, authenticated using (true);

-- CMS master-file product data (name, image, category) for the SKU list page.
-- Populated by imax_masterfile_to_supabase.py from the CMS masterFile API
-- (brand 76471 for B0812001); slow-changing, daily refresh is enough.
create table if not exists imax_sku_master (
  store_code text not null, sku_id text not null, sku_code text, name_zh text,
  name_en text, image_url text, cat_l1 text, cat_l2 text, cat_l3 text,
  master_file text, active boolean, original_price numeric, discount_price numeric,
  updated_at timestamptz not null default now(), primary key (store_code, sku_id));
alter table imax_sku_master enable row level security;
create policy m_read on imax_sku_master for select to anon, authenticated using (true);
