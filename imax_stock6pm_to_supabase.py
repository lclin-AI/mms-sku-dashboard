"""
iMAX 6PM physical-stock snapshot -> Supabase, per SKU per day.

"Remaining" in the inventory tracker = actual physical stock at 6PM, EXCLUDING
R & X locations (returns / picking-staging). This snapshots iMAX
getStockInfoByLocation and sums physicalStock per SKU, excluding locations that
start with R or X (also HSCC and imaxOnly rows, per the stock-health tooling).

The snapshot is dated by the day it is taken (run this at ~18:00 daily). The
tracker page shows, for date D, the snapshot from D-1 (yesterday's 6PM close).
It cannot be backfilled: only days from the first run onward will have a value.

MUST run locally on the internal network (iMAX is WAF + internal only).

Usage: python imax_stock6pm_to_supabase.py --store B0812001
"""
import argparse, json, os, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests

IMAX = "https://imax.hktvmall.com/hktv_imax"
HK = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(HERE, "imax_session.json")
IMAX_CFG = r"C:\Users\lclin\imax-stock-slack\config.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def load_run_env():
    p = os.path.join(HERE, "run.env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()


def imax_creds():
    u = os.environ.get("IMAX_USERNAME"); p = os.environ.get("IMAX_PASSWORD")
    if u and p:
        return u, p
    if os.path.exists(IMAX_CFG):
        d = json.load(open(IMAX_CFG, encoding="utf-8"))
        return d.get("imaxUser"), d.get("imaxPass")
    return None, None


def imax_get(path, params):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json", "Referer": IMAX + "/"})
    if os.path.exists(SESSION_FILE):
        s.cookies.update(json.load(open(SESSION_FILE)))
        r = s.get(IMAX + path, params=params, timeout=120)
        if r.status_code == 200:
            return r
    u, p = imax_creds()
    if not (u and p):
        sys.exit("No iMAX creds.")
    s.cookies.clear()
    lr = s.post(IMAX + "/login", data={"username": u, "password": p},
                headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    if lr.status_code != 200 or not s.cookies:
        sys.exit(f"iMAX login failed (HTTP {lr.status_code}).")
    json.dump({c.name: c.value for c in s.cookies}, open(SESSION_FILE, "w"))
    return s.get(IMAX + path, params=params, timeout=120)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="B0812001")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    load_run_env()

    sb_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not a.dry_run and not (sb_url and sb_key):
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

    print("Fetching iMAX stock by location...", file=sys.stderr)
    r = imax_get("/frontend/getStockInfoByLocation", {"material": a.store})
    if r.status_code != 200:
        sys.exit(f"stock fetch failed (HTTP {r.status_code}).")
    arr = r.json()
    arr = arr if isinstance(arr, list) else arr.get("data", [])

    prefix = a.store + "-"
    agg = defaultdict(float)
    for x in arr:
        mc = str(x.get("material") or "")
        # getStockInfoByLocation keys SKUs by hybrisSkuId (e.g. B0812001_S_00002)
        sku = None
        h = str(x.get("hybrisSkuId") or "")
        if "_S_" in h:
            sku = h.split("_S_")[-1]
        elif mc.startswith(prefix):
            sku = mc[len(prefix):]
        if sku is None:
            continue
        loc = str(x.get("location") or "").upper()
        if loc.startswith("R") or loc.startswith("X") or loc == "HSCC":
            continue
        if x.get("imaxOnly"):
            continue
        agg[sku] += float(x.get("physicalStock") or 0)

    today = datetime.now(HK).strftime("%Y-%m-%d")
    rows = [{"store_code": a.store, "date": today, "sku_id": sku, "phys_qty": round(q, 3)}
            for sku, q in agg.items()]
    print(f"{len(rows)} SKUs snapshotted for {today}", file=sys.stderr)
    if a.dry_run:
        for r0 in sorted(rows, key=lambda z: -z["phys_qty"])[:15]:
            print("  ", r0, file=sys.stderr)
        return

    sb = requests.Session()
    sb.headers.update({"apikey": sb_key, "Authorization": "Bearer " + sb_key,
                       "Content-Type": "application/json"})
    for i in range(0, len(rows), 500):
        resp = sb.post(f"{sb_url}/rest/v1/imax_stock_6pm",
                       headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                       json=rows[i:i + 500], timeout=120)
        if resp.status_code >= 300:
            sys.exit(f"Supabase write failed {resp.status_code}: {resp.text[:300]}")
    print(f"upserted {len(rows)} 6PM stock rows for {today}", file=sys.stderr)


if __name__ == "__main__":
    main()
