"""
SAFE no-op test for the MMS manual-adjust write path.

Reads SKU 00001's current warehouse stock, PUTs mode=set with the SAME qty
(net change = 0), then reads again to confirm it is unchanged. Proves the
/warehouse endpoint + payload work WITHOUT changing any inventory.

Uses the "other account" (creds_adjust.json) with its own session file, so it
does not touch the pycheung session.

Run:
  python adjust_test.py
"""
import os, json, sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("MMS_CREDS_FILE", os.path.join(HERE, "creds_adjust.json"))
os.environ.setdefault("MMS_STATE_FILE", os.path.join(HERE, "mms_adjust_state.json"))

import mms_login, requests

BASE = "https://merchant-web.shoalter.com/inventory/api/v2/product-inventory"
UUID = "eacb454b-cb3c-4338-999a-32019da1baab"   # B0812001 SKU 00001 蕃茄


def detail(H):
    r = requests.get(f"{BASE}/{UUID}", headers=H, timeout=90).json()
    d = (r.get("response") or {}).get("buInventoryDetails") or []
    if not d:
        sys.exit("detail: no buInventoryDetails (token scope / uuid issue)")
    bu = d[0]
    st = bu["stockInfoList"][0]
    return bu, st


def main():
    print("logging in (headless browser, ~15-30s, no output until done)...", flush=True)
    tok = mms_login.get_token()
    print("login OK, reading current stock...", flush=True)
    H = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}

    bu, st = detail(H)
    cur = st["stockQty"]
    wsn = st["warehouseSeqNo"]
    print(f"BEFORE : stockQty={cur}  warehouseSeqNo={wsn}  storeSkuId={bu['storeSkuId']}  prm={bu['productReadyMethod']}")

    payload = {"uuid": UUID, "productReadyMethod": bu["productReadyMethod"],
               "warehouseList": [{"warehouseSeqNo": wsn, "storeId": bu["storeId"],
                                  "storeSkuId": bu["storeSkuId"],
                                  "mode": "set", "qty": cur}]}
    print("PUT    :", json.dumps(payload))
    r = requests.put(f"{BASE}/warehouse", headers=H, data=json.dumps(payload), timeout=90)
    print(f"PUT rc : {r.status_code}  body={r.text[:300]}")

    _, st2 = detail(H)
    print(f"AFTER  : stockQty={st2['stockQty']}")
    ok = (r.status_code < 300 and st2["stockQty"] == cur)
    print("RESULT :", "OK  endpoint works, net change = 0" if ok
          else "CHECK  (status or qty unexpected)")


if __name__ == "__main__":
    main()
