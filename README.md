# MMS SKU Sales Dashboard

Static dashboard (GitHub Pages) that reads SKU-level sales out of Supabase.
Pick a SKU from the dropdown and it shows that SKU's daily sales performance.

```
MMS Daily Order Report API  ──(mms_to_supabase.py, daily)──>  Supabase
                                                                  │
                                       GitHub Pages (index.html) ─┘  read-only, anon key
```

## Why a pipeline and not a direct call

The dashboard cannot call MMS directly: `merchant-web.shoalter.com` is
cross-origin with no CORS for a Pages host, and the `accessToken` is a
short-lived JWT that cannot be embedded in a public page. Supabase is the
buffer — and it also solves MMS's **~12-day report retention**, which is the
real reason to archive: anything older is gone at source.

## Setup

1. **Supabase** — run `schema.sql` in the SQL editor. It creates
   `mms_sku_daily` (grain: store × order_date × delivery_date × sku),
   the `mms_sku_catalog` view for the dropdown, and an anon **read-only** policy.

2. **Load data**
   ```
   set MMS_TOKEN=<accessToken cookie from merchant.shoalter.com>
   set SUPABASE_URL=https://<ref>.supabase.co
   set SUPABASE_SERVICE_ROLE_KEY=<service_role key>
   pip install requests openpyxl
   python mms_to_supabase.py --store B0812001 --days 12
   ```
   Re-running is safe: each order_date's rows are replaced, not duplicated.

3. **Dashboard** — put the project URL and the **anon** key in `config.js`,
   then enable GitHub Pages on the repo (Settings → Pages → deploy from branch).

## Security: which Supabase project

The anon key ships inside a public page, so **everything anon can reach is
public**. Use a project whose only anon-visible surface is this read-only table.

Do **not** reuse a project that has permissive anon write policies. Check with:

```sql
select tablename, policyname, permissive, roles::text, cmd
from pg_policies where schemaname='public';
```

A `PERMISSIVE` policy with `cmd = ALL` and `anon` in roles means anyone holding
the key can INSERT/UPDATE/DELETE that table.

## Getting a fresh MMS token

DevTools on merchant.shoalter.com → Application → Cookies → `accessToken`.
It expires in well under an hour. On expiry the API answers `code: FAIL`,
`"Error. Unable to get information from mms-user."` — that means 401, not a bug.

## Data notes

- Rows come from the **Daily Order Report** (`/order/{bu}/{store}/DAILY/report`),
  which is SKU-level and includes unit price, discount and line total.
- Files are keyed on **order date**; each row carries its own delivery date, so
  the dashboard can group either way (the 落單日 / 送貨日 toggle).
- Today's file is a **12:00 snapshot**; past days are full-day (`235959`).
  Today's bar therefore reads low until the next run.
- The report contains customer PII (name, phone, address). The uploader
  deliberately keeps **none** of it — only SKU, dates, qty and money.
