"""
MMS EXPRESS (倉存即送 / productReadyMethod C) sales -> Supabase, per SKU per delivery date.

The MMS Daily Order Report only contains standard (H) orders and OMITS express
(EM) orders, which are the wet market's main channel. Express orders live in
/order/v2/consignments (productReadyMethod C); their SKU lines come from
/order/v2/{consignmentCode}/consignmentDetails.

This pulls express consignments delivering within the window, sums SKU quantity
per (delivery_date, sku), and upserts mms_express_daily. The inventory tracker
adds this to the standard sold quantity so 已售 reflects real MMS transactions
(express included). Cancelled consignments are excluded.

MUST run locally on the internal network (MMS uses the browser-login token; heavy
per-consignment detail calls). Today changes intraday, so run it often for a
short window plus a daily backfill.

Usage:
  python mms_express_to_supabase.py --store B0812001 --days 1
  python mms_express_to_supabase.py --store B0812001 --days 12
"""
import argparse, json, os, sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import mms_login, requests

MMS = "https://merchant-web.shoalter.com"
HK = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))

ACTIVE_STATUS = ["CONFIRMED", "ACKNOWLEDGED", "PACKED", "PICKED",
                 "PICKEDUP_FROM_MERCHANT", "IN_HUB", "DISPATCHED", "IN_STORE",
                 "IN_LOCKER", "MERCHANT_SHIPPED", "FAIL_TO_DELIVER",
                 "HOLD_BY_CS", "RELEASE_BY_CS", "RECEIVED_BY_CUSTOMER",
                 "ORDER_COMPLETE"]   # excludes CANCELLED / CS_CANCEL*


def load_run_env():
    p = os.path.join(HERE, "run.env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()


def hk_ms(d, end=False):
    dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=HK)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def hk_date(ms):
    return datetime.fromtimestamp(ms / 1000, HK).strftime("%Y-%m-%d") if ms else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="B0812001")
    ap.add_argument("--days", type=int, default=1,
                    help="delivery-date window back from today")
    ap.add_argument("--fwd", type=int, default=14,
                    help="delivery-date window forward from today")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    load_run_env()

    sb_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not a.dry_run and not (sb_url and sb_key):
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

    tok = mms_login.get_token()
    mms = requests.Session()
    mms.headers.update({"Authorization": "Bearer " + tok, "Content-Type": "application/json"})

    today = datetime.now(HK).date()
    d0 = today - timedelta(days=a.days - 1)
    d1 = today + timedelta(days=a.fwd)   # deliveries can be scheduled forward
    wh = [f"{a.store}{i:02d}" for i in range(1, 100)]

    # 1) list express consignments delivering in the window
    consigns = []   # (consignmentCode, delivery_date)
    page = 1
    while True:
        body = {"storefrontStoreCodes": [a.store], "productReadyMethods": ["C"],
                "warehouseCodes": wh, "startDate": hk_ms(d0), "endDate": hk_ms(d1, True),
                "status": ACTIVE_STATUS, "searchDateType": "DELIVERY_DATE",
                "deliveryMode": "STANDARD_DELIVERY", "sortColumn": "ISSUE_DATE",
                "sortDirection": "DESC", "searchType": "ORDER_ID", "searchKeyword": "",
                "pageNumber": page, "pageSize": 1000}
        j = mms.post(f"{MMS}/order/v2/consignments", data=json.dumps(body), timeout=90).json()
        resp = j.get("response") or {}
        for x in resp.get("data") or []:
            consigns.append((x.get("consignmentCode"), hk_date(x.get("deliveryDate"))))
        pg = resp.get("pagination") or {}
        if page >= pg.get("numberOfPages", 1):
            break
        page += 1
    print(f"{len(consigns)} express consignments in window", file=sys.stderr)

    # 2) SKU lines per consignment
    agg = defaultdict(lambda: {"qty": 0.0, "wb": 0})   # (delivery_date, sku) -> qty

    def fetch(item):
        code, dd = item
        try:
            r = mms.get(f"{MMS}/order/v2/{code}/consignmentDetails", timeout=60)
            entries = (r.json().get("data") or {}).get("consignmentEntries") or []
            return dd, entries
        except Exception:
            return dd, None

    done = fail = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for dd, entries in ex.map(fetch, consigns):
            if entries is None:
                fail += 1
                continue
            done += 1
            for e in entries:
                sku = str(e.get("skuId") or "")
                if "_S_" in sku:
                    sku = sku.split("_S_")[-1]
                k = (dd, sku)
                agg[k]["qty"] += float(e.get("quantity") or 0)
                agg[k]["wb"] += 1
    print(f"details ok={done} fail={fail}; {len(agg)} (date,sku) rows", file=sys.stderr)

    rows = [{"store_code": a.store, "delivery_date": dd, "sku_id": sku,
             "qty": round(v["qty"], 3), "waybills": v["wb"],
             "updated_at": datetime.now(timezone.utc).isoformat()}
            for (dd, sku), v in agg.items() if dd]
    if a.dry_run:
        print("sample:", rows[:3], file=sys.stderr)
        return

    sb = requests.Session()
    sb.headers.update({"apikey": sb_key, "Authorization": "Bearer " + sb_key,
                       "Content-Type": "application/json",
                       "Prefer": "resolution=merge-duplicates,return=minimal"})
    # authoritative replace for the delivery-date window
    sb.delete(f"{sb_url}/rest/v1/mms_express_daily",
              params={"store_code": f"eq.{a.store}",
                      "delivery_date": f"gte.{d0.isoformat()}"}, timeout=60).raise_for_status()
    for i in range(0, len(rows), 500):
        resp = sb.post(f"{sb_url}/rest/v1/mms_express_daily", json=rows[i:i + 500], timeout=120)
        if resp.status_code >= 300:
            sys.exit(f"write failed {resp.status_code}: {resp.text[:300]}")
    print(f"wrote {len(rows)} express rows for {a.store}", file=sys.stderr)


if __name__ == "__main__":
    main()
