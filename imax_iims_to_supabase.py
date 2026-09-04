"""
IIMS stock-levels -> Supabase, per SKU (and by-date when IIMS provides it).

For each store SKU, calls
  GET https://iims-restful.shoalter.com/iims/s2s/v2/hybris/products/<store>_S_<sku>/stock-levels
and stores:
  - imax_iims_sku:    one row {quantity (total), has_bydate, update_stock_time}
  - imax_iims_bydate: if the response has dateInventory[], one row per date

The tracker page shows IIMS per-date when has_bydate, else the single quantity.

MUST run locally (IIMS s2s is not browser/CORS reachable and is internal/partner
network). Light per call, but ~400 SKUs, so runs threaded.

Usage: python imax_iims_to_supabase.py --store B0812001
"""
import argparse, json, os, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

NOW_ISO = datetime.now(timezone.utc).isoformat()

IIMS = "https://iims-restful.shoalter.com/iims/s2s/v2/hybris/products/{}/stock-levels"
HK = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))


def load_run_env():
    p = os.path.join(HERE, "run.env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()


def sku_list(sb_url, sb_key, store):
    """All store SKU ids: union of sold (catalog), PO'd (imax_daily) and
    physically-stocked (6PM snapshot) — the sold catalog alone misses SKUs like
    10040 that have IIMS by-date inventory but no recent sales."""
    h = {"apikey": sb_key, "Authorization": "Bearer " + sb_key}
    out = set()
    for table in ("mms_sku_catalog", "imax_daily", "imax_stock_6pm"):
        r = requests.get(f"{sb_url}/rest/v1/{table}",
                         params={"store_code": f"eq.{store}", "select": "sku_id", "limit": 100000},
                         headers=h, timeout=60)
        r.raise_for_status()
        out.update(row["sku_id"] for row in r.json())
    return sorted(out)


def iso(yyyymmdd):
    s = str(yyyymmdd)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else None


def fetch(store, sku):
    full = f"{store}_S_{sku}"
    try:
        r = requests.get(IIMS.format(full), timeout=20,
                         headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        if r.status_code != 200:
            return sku, None
        return sku, (r.json() or {}).get("data")
    except Exception:
        return sku, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="B0812001")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    load_run_env()

    sb_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not (sb_url and sb_key):
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

    skus = sku_list(sb_url, sb_key, a.store)
    print(f"{len(skus)} SKUs to query IIMS", file=sys.stderr)

    sku_rows, bydate_rows = [], []
    ok = miss = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for sku, data in ex.map(lambda s: fetch(a.store, s), skus):
            if not data:
                miss += 1
                continue
            ok += 1
            di = data.get("dateInventory") or []
            has = bool(di)
            sku_rows.append({"store_code": a.store, "sku_id": sku,
                             "quantity": data.get("quantity"), "has_bydate": has,
                             "update_stock_time": data.get("updateStockTime"),
                             "updated_at": NOW_ISO})
            for e in di:
                d = iso(e.get("date"))
                if d:
                    bydate_rows.append({"store_code": a.store, "sku_id": sku,
                                        "date": d, "quantity": e.get("quantity")})
    print(f"ok={ok} miss={miss}; {len(bydate_rows)} by-date rows", file=sys.stderr)
    if a.dry_run:
        print("sample sku rows:", sku_rows[:3], file=sys.stderr)
        print("sample bydate:", bydate_rows[:3], file=sys.stderr)
        return

    sb = requests.Session()
    sb.headers.update({"apikey": sb_key, "Authorization": "Bearer " + sb_key,
                       "Content-Type": "application/json",
                       "Prefer": "resolution=merge-duplicates,return=minimal"})

    def upsert(table, rows):
        for i in range(0, len(rows), 500):
            resp = sb.post(f"{sb_url}/rest/v1/{table}", json=rows[i:i + 500], timeout=120)
            if resp.status_code >= 300:
                sys.exit(f"{table} write failed {resp.status_code}: {resp.text[:300]}")

    upsert("imax_iims_sku", sku_rows)
    upsert("imax_iims_bydate", bydate_rows)
    print(f"upserted {len(sku_rows)} sku rows, {len(bydate_rows)} by-date rows", file=sys.stderr)


if __name__ == "__main__":
    main()
