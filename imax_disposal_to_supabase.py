"""
iMAX disposal (棄置) -> Supabase, per SKU per day.

Definition (per user): net quantity moved to locations whose code starts with
"R", summed per (process day, sku). Source: iMAX getMaterialProcessLog, which
logs every stock movement with processDate / location / qty / txDescription.

NOTE: R-location net qty mixes movement types (change_location, goods_issue,
sap_disposal). It can be positive on some days. If this proves too noisy, the
cleaner "pure disposal" signal is txDescription == 'sap_disposal' — switch by
setting MODE below.

Writes disposal_qty into imax_daily via upsert (merge on PK), leaving po_qty /
gr_qty (maintained by imax_to_supabase.py) untouched.

MUST run locally on the internal network (iMAX is WAF + internal only).
Heavier than the PO/GR pull (the process log is large), so schedule it less
often, e.g. every 30 min.

Usage:
  python imax_disposal_to_supabase.py --store B0812001 --days 14
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

MODE = "R_LOCATION"   # or "SAP_DISPOSAL"

# Exact disposal locations to record (provided by the user). Disposal = net qty
# of location-change movements INTO any of these that day (in − moved back out),
# excluding the sap_disposal write-off.
DISPOSAL_LOCATIONS = {
    "R008", "R668", "R669", "R674", "R675", "R676", "R677", "R678", "R679",
    "R680", "R681", "R682", "R683", "R684", "R685", "R686", "R687", "R688",
    "R689", "R690", "R691", "R692", "R693", "R694", "R695", "R696", "R697",
    "R699", "X015", "X168", "X102", "R701", "R702", "R703", "R704", "R711",
    "R712", "R713", "R714", "XPAC", "XSCC", "R004", "X713", "ROFF-TAC-CRAB",
    "R014", "X016", "ROFF-TY-INTERLINK", "R511", "R512", "R513", "R514", "R515",
    "R516", "R517", "R518", "R519", "R521", "R522", "R523", "R524", "R525",
    "R526", "R527", "R540", "R541", "R542", "R543", "R544", "R545", "R546",
    "R547", "R548", "R549", "R550", "R551", "R552", "R553", "R554", "R556",
    "R557", "R558", "R559",
}


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
    s.headers.update({"User-Agent": UA, "Accept": "application/json",
                      "Referer": IMAX + "/"})
    if os.path.exists(SESSION_FILE):
        s.cookies.update(json.load(open(SESSION_FILE)))
        r = s.get(IMAX + path, params=params, timeout=180)
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
    return s.get(IMAX + path, params=params, timeout=180)


def day(ms):
    # Business day with a 4PM cutoff: a movement processed at/after 16:00 HK
    # counts towards the NEXT day (that day's operations start after the cutoff).
    if not ms:
        return None
    dt = datetime.fromtimestamp(ms / 1000, HK)
    if dt.hour >= 16:
        dt = dt + timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="B0812001")
    ap.add_argument("--days", type=int, default=14,
                    help="how many days back of disposal to refresh")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    load_run_env()

    sb_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not a.dry_run and not (sb_url and sb_key):
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

    now = datetime.now(HK)
    frm = int((now - timedelta(days=a.days)).replace(hour=0, minute=0, second=0,
                                                     microsecond=0).timestamp() * 1000)
    to = int(now.timestamp() * 1000)

    print(f"Fetching iMAX process log ({a.days}d, material={a.store})...", file=sys.stderr)
    r = imax_get("/frontend/getMaterialProcessLog",
                 {"from": frm, "to": to, "material": a.store})
    if r.status_code != 200:
        sys.exit(f"process log fetch failed (HTTP {r.status_code}).")
    arr = r.json()
    arr = arr if isinstance(arr, list) else arr.get("data", [])
    print(f"{len(arr)} movement rows", file=sys.stderr)

    prefix = a.store + "-"
    agg = defaultdict(float)  # (day, sku) -> disposal qty
    for x in arr:
        mc = str(x.get("material") or "")
        if not mc.startswith(prefix):
            continue
        loc = str(x.get("location") or "").upper()
        tx = x.get("txDescription")
        q = float(x.get("qty") or 0)
        if MODE == "R_LOCATION":
            # Net quantity moved to the listed disposal locations that day via
            # location changes: moved INTO a disposal location (+) MINUS moved
            # back OUT that same day (−). Exclude sap_disposal (SAP write-off of
            # the same stock, lands on a later day).
            if loc not in DISPOSAL_LOCATIONS or tx == "sap_disposal":
                continue
            val = q
        else:  # SAP_DISPOSAL: the write-off itself (negative), reported positive
            if tx != "sap_disposal":
                continue
            val = -q
        d = day(x.get("processDate"))
        if not d:
            continue
        agg[(d, mc[len(prefix):])] += val

    rows = [{"store_code": a.store, "date": d, "sku_id": sku,
             "disposal_qty": round(q, 3)}
            for (d, sku), q in agg.items()]
    print(f"{len(rows)} (day,sku) disposal rows (mode={MODE})", file=sys.stderr)
    if a.dry_run:
        for r0 in sorted(rows, key=lambda z: (z["date"], z["sku_id"]))[:20]:
            print("  ", r0, file=sys.stderr)
        return

    sb = requests.Session()
    sb.headers.update({"apikey": sb_key, "Authorization": "Bearer " + sb_key,
                       "Content-Type": "application/json"})
    for i in range(0, len(rows), 500):
        resp = sb.post(f"{sb_url}/rest/v1/imax_daily",
                       headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                       json=rows[i:i + 500], timeout=120)
        if resp.status_code >= 300:
            sys.exit(f"Supabase write failed {resp.status_code}: {resp.text[:300]}")
    print(f"upserted {len(rows)} disposal rows for {a.store}", file=sys.stderr)


if __name__ == "__main__":
    main()
