"""
iMAX Purchase Order / Goods Receipt -> Supabase (per SKU, per booking date).

Pulls iMAX getPurchaseOrderInfo and writes, for the store's materials:
  po_qty = sum(orderQuantity)      grouped by (booking date, sku)
  gr_qty = sum(receivedQuantity)   grouped by (booking date, sku)
CANCELLED POs are excluded. Lines with no booking date are NOT dated (they are
reported separately by --show-nobooking); the per-date table only shows booked
PO/GR, matching the MMS inventory view.

MUST run on the user's PC on the internal network: iMAX is behind Imperva WAF
and is internal-only, so this cannot run in the cloud.

Creds: IMAX_USERNAME / IMAX_PASSWORD env, else imax-stock-slack\\config.json
(imaxUser / imaxPass). Supabase from run.env (SUPABASE_URL,
SUPABASE_SERVICE_ROLE_KEY) or env.

Usage:
  python imax_to_supabase.py --store B0812001
  python imax_to_supabase.py --store B0812001 --show-nobooking
"""
import argparse, json, os, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests

IMAX = "https://imax.hktvmall.com/hktv_imax"
RMCODE = "E0059"
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


def imax_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json",
                      "Referer": IMAX + "/imax/purchaseOrder?rmCode=" + RMCODE})
    if os.path.exists(SESSION_FILE):
        s.cookies.update(json.load(open(SESSION_FILE)))
        r = s.get(IMAX + "/frontend/getPurchaseOrderInfo",
                  params={"rmCode": RMCODE}, timeout=30)
        if r.status_code == 200:
            try:
                r.json(); return s, r
            except Exception:
                pass
    u, p = imax_creds()
    if not (u and p):
        sys.exit("No iMAX creds (set IMAX_USERNAME/PASSWORD or imax-stock-slack config).")
    s.cookies.clear()
    lr = s.post(IMAX + "/login", data={"username": u, "password": p},
                headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    if lr.status_code != 200 or not s.cookies:
        sys.exit(f"iMAX login failed (HTTP {lr.status_code}).")
    json.dump({c.name: c.value for c in s.cookies}, open(SESSION_FILE, "w"))
    r = s.get(IMAX + "/frontend/getPurchaseOrderInfo",
              params={"rmCode": RMCODE}, timeout=60)
    return s, r


def hk_date(ms):
    return datetime.fromtimestamp(ms / 1000, HK).strftime("%Y-%m-%d") if ms else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="B0812001")
    ap.add_argument("--days-back", type=int, default=90,
                    help="only load PO/GR whose booking date is within this many "
                         "days of today (keeps the 5-min sync light)")
    ap.add_argument("--show-nobooking", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    load_run_env()

    from datetime import date as _date
    cutoff = (datetime.now(HK).date() - timedelta(days=a.days_back)).isoformat()

    sb_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not a.dry_run and not (sb_url and sb_key):
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

    print("Logging in to iMAX / fetching PO...", file=sys.stderr)
    _, r = imax_session()
    if r.status_code != 200:
        sys.exit(f"PO fetch failed (HTTP {r.status_code}).")
    arr = r.json()
    arr = arr if isinstance(arr, list) else arr.get("data", [])
    print(f"{len(arr)} PO lines total", file=sys.stderr)

    prefix = a.store + "-"
    dated = defaultdict(lambda: {"po": 0.0, "gr": 0.0})   # (date, sku) -> qty
    nobook = defaultdict(lambda: {"po": 0.0, "gr": 0.0})  # sku -> qty
    for x in arr:
        mc = str(x.get("materialCode") or "")
        if not mc.startswith(prefix):
            continue
        if str(x.get("status")) == "CANCELLED":
            continue
        sku = mc[len(prefix):]
        oq = float(x.get("orderQuantity") or 0)
        rq = float(x.get("receivedQuantity") or 0)
        bd = hk_date(x.get("bookedDate"))
        if bd:
            if bd < cutoff:
                continue  # older than the window we keep
            d = dated[(bd, sku)]; d["po"] += oq; d["gr"] += rq
        else:
            n = nobook[sku]; n["po"] += oq; n["gr"] += rq

    rows = [{"store_code": a.store, "date": dt, "sku_id": sku,
             "po_qty": round(v["po"], 3), "gr_qty": round(v["gr"], 3)}
            for (dt, sku), v in dated.items()]
    print(f"{len(rows)} dated (sku,date) rows; {len(nobook)} SKUs with no-booking POs",
          file=sys.stderr)

    if a.show_nobooking:
        print("--- no-booking PO/GR by SKU ---", file=sys.stderr)
        for sku, v in sorted(nobook.items(), key=lambda kv: -kv[1]["po"])[:40]:
            print(f"  {sku}: PO {v['po']:.0f}  GR {v['gr']:.0f}", file=sys.stderr)

    if a.dry_run:
        print("dry run, nothing written", file=sys.stderr)
        return

    sb = requests.Session()
    sb.headers.update({"apikey": sb_key, "Authorization": "Bearer " + sb_key,
                       "Content-Type": "application/json"})
    # Replace this store's PO/GR wholesale (idempotent). Preserve any disposal_qty
    # already stored by upserting po/gr only would be complex; instead delete+insert
    # keeps po/gr authoritative. Disposal is loaded by a separate job that upserts
    # its own column, so run order: PO/GR first, then disposal.
    # Upsert po/gr only (merge on the PK), so we never touch disposal_qty, which
    # is maintained by imax_disposal_to_supabase.py. Missing rows are created with
    # disposal_qty NULL; on conflict only po_qty/gr_qty change.
    for i in range(0, len(rows), 500):
        resp = sb.post(f"{sb_url}/rest/v1/imax_daily",
                       headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                       json=rows[i:i + 500], timeout=120)
        if resp.status_code >= 300:
            sys.exit(f"Supabase write failed {resp.status_code}: {resp.text[:300]}")
    print(f"upserted {len(rows)} po/gr rows to imax_daily for {a.store}", file=sys.stderr)


if __name__ == "__main__":
    main()
