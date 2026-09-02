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

## Truly automated daily (Windows Task Scheduler)

The MMS accessToken lives under an hour, so the pipeline logs in fresh on every
run with Playwright — driving the real sign-in page, not any internal token
endpoint. A saved browser session (`mms_state.json`) means most runs skip the
password step and just refresh.

```
07:00  run_daily.bat  ->  mms_login.get_token()  ->  mms_to_supabase.py (per store)
07:40  run_monitor.bat -> checks Supabase freshness, Slack-alerts if the load failed
```

### One-time setup

1. `pip install requests openpyxl playwright && python -m playwright install chromium`
2. Copy `run.env.example` → `run.env`, fill in `SUPABASE_SERVICE_ROLE_KEY`
   (from the **new** project's Settings → API → service_role), your `MMS_STORES`,
   and MMS `MMS_USERCODE` / `MMS_PASSWORD` (or `creds.json`).
3. **Supervised first login** — clears any captcha/2FA and seeds the session:
   ```
   python mms_login.py --headed
   ```
   It should print a token and write `mms_state.json`.
4. Dry-run the full load once:
   ```
   python run_daily.py
   ```
   Then reload the dashboard — data should appear.
5. Register the schedules (elevated PowerShell):
   ```
   powershell -ExecutionPolicy Bypass -File register_tasks.ps1
   ```

### Notes

- The load and the ETL run **on this machine**, so the MMS token and the Supabase
  service_role key never leave it — only SKU/date/qty/amount rows go to Supabase.
- Uses the WindowsApps Python alias, which needs the user profile; the tasks run
  as the current user. The machine must be on around 07:00 (Task Scheduler's
  *Start when available* catches a missed run once it powers on).
- `mms_monitor.py` reuses the Slack bot token in `imax-stock-slack\config.json`;
  set `SLACK_TOKEN` / `SLACK_CHANNEL` to override, or it just prints.
- If MMS changes its login DOM, `mms_login.py` writes `login_failed.png` — run
  `--headed` to see what happened and adjust selectors.

### Files (operational — gitignored secrets)

| file | role |
|------|------|
| `mms_login.py`     | Playwright headless login → accessToken (+ saved session) |
| `run_daily.py`     | orchestrator: login → ETL per store |
| `mms_to_supabase.py` | the ETL (also usable standalone with a manual token) |
| `mms_monitor.py`   | freshness check + Slack alert |
| `run_daily.bat` / `run_monitor.bat` | Task Scheduler entry points |
| `register_tasks.ps1` | one-shot scheduled-task registration |
| `run.env`, `creds.json`, `mms_state.json` | local secrets/state, never committed |
