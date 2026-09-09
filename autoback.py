"""Auto add-back inventory. Runs right after the MMS sync (same 5-min job).

For each SKU in autoback_config it computes soldAfterD = the dashboard 已售
(express + standard) summed over delivery dates STRICTLY AFTER the configured
delivery date D. The incremental is (soldAfterD now) - (soldAfterD at the
previous run), stored for the dashboard's verify field. If the SKU is enabled
and the incremental is positive, that many units (capped at the expected qty Q)
are added back to MMS via the mms-adjust Edge Function (which logs to
adjust_log). Negative incrementals (refunds/cancels) are ignored.

Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ADJUST_PASSWORD, and (from
config.js) the Edge Function URL + anon key.
"""
import os, re, sys, json
from datetime import datetime, timezone
import requests

HERE = os.path.dirname(os.path.abspath(__file__))


def load_run_env():
    p = os.path.join(HERE, "run.env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main():
    load_run_env()
    sb = os.environ.get("SUPABASE_URL", "").rstrip("/")
    sr = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    pw = os.environ.get("ADJUST_PASSWORD", "")
    if not (sb and sr):
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

    cfgjs = open(os.path.join(HERE, "config.js"), encoding="utf-8").read()
    fn_url = re.search(r'ADJUST_FN_URL:\s*"([^"]+)"', cfgjs).group(1)
    anon = re.search(r'SUPABASE_ANON_KEY:\s*"([^"]+)"', cfgjs).group(1)

    SR = {"apikey": sr, "Authorization": "Bearer " + sr}
    RW = {**SR, "Content-Type": "application/json",
          "Prefer": "resolution=merge-duplicates,return=minimal"}

    def get(path):
        return requests.get(f"{sb}/rest/v1/{path}", headers=SR, timeout=60).json()

    cfg = get("autoback_config?select=store_code,sku_id,enabled,delivery_date,expected_qty")
    if not cfg:
        print("no autoback_config rows", file=sys.stderr)
        return
    stores = sorted(set(c["store_code"] for c in cfg))

    # snapshot state
    state = {(s["store_code"], s["sku_id"]): s
             for s in get("autoback_state?select=store_code,sku_id,sold_after_d")}

    for store in stores:
        # all sold per (sku, delivery_date) for this store
        sold = {}
        for tbl in ("mms_express_daily", "mms_sku_daily"):
            for r in get(f"{tbl}?store_code=eq.{store}&select=sku_id,delivery_date,qty&limit=100000"):
                sold.setdefault(r["sku_id"], {})
                sold[r["sku_id"]][r["delivery_date"]] = \
                    sold[r["sku_id"]].get(r["delivery_date"], 0) + float(r["qty"])

        for c in [c for c in cfg if c["store_code"] == store]:
            sku = c["sku_id"]; D = c["delivery_date"]; Q = float(c["expected_qty"])
            after_d = sum(q for dt, q in sold.get(sku, {}).items() if dt > D)
            prev = state.get((store, sku), {}).get("sold_after_d")
            incremental = None if prev is None else (after_d - float(prev))
            add_done = 0.0

            if incremental is not None and incremental > 0 and c["enabled"] and pw:
                add_qty = min(incremental, Q)   # cap at the expected qty
                try:
                    r = requests.post(fn_url, timeout=90,
                        headers={"Content-Type": "application/json",
                                 "apikey": anon, "Authorization": "Bearer " + anon},
                        data=json.dumps({"store_code": store, "sku_id": sku,
                                         "mode": "add", "qty": add_qty,
                                         "password": pw, "operator": "auto-addback"}))
                    j = r.json()
                    if j.get("ok"):
                        add_done = add_qty
                        print(f"{sku}: +{add_qty:.0f} (incr {incremental:.0f}) -> {j.get('after')}", file=sys.stderr)
                    else:
                        print(f"{sku}: add failed {r.status_code} {str(j)[:120]}", file=sys.stderr)
                except Exception as e:
                    print(f"{sku}: add error {type(e).__name__} {str(e)[:100]}", file=sys.stderr)

            requests.post(f"{sb}/rest/v1/autoback_state", headers=RW, timeout=30,
                data=json.dumps({"store_code": store, "sku_id": sku,
                                 "sold_after_d": after_d,
                                 "last_incremental": incremental,
                                 "last_added": add_done,
                                 "last_run_at": datetime.now(timezone.utc).isoformat()}))


if __name__ == "__main__":
    main()
