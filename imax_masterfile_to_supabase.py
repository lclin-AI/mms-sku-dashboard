"""
CMS master-file SKUs -> Supabase (name, image, category) for the SKU list page.

Enumerates every master-file SKU of the store's CMS brand via
  GET /api/express/cms/v1/sku/masterFile/brand?brandId=<brand>
  GET /api/express-order/cms/v2/master-file-sku/simplifySearch?masterFile=..&pageSize=200&currentPage=n
and upserts per SKU: name, image url, category (levels 1-3), active, prices.
skuCode is "<brand>_S_<suffix>"; suffix == MMS sku_id (join key).

CMS token (api.takeaway.hktvmall.com Bearer): from env CMS_TOKEN, else read from
the CMS Supabase project (settings.cms_token) via CMS_SB_URL / CMS_SB_KEY.
Writes to the dashboard project via run.env SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY.

Master data is slow-changing; a daily refresh is plenty.

Usage:
  python imax_masterfile_to_supabase.py --store B0812001 --brand 76471
"""
import argparse, json, os, sys
import requests

CMS = "https://api.takeaway.hktvmall.com"
HERE = os.path.dirname(os.path.abspath(__file__))


def load_run_env():
    p = os.path.join(HERE, "run.env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()


def cms_token():
    t = os.environ.get("CMS_TOKEN")
    if t:
        return t
    url = os.environ.get("CMS_SB_URL", "https://kipiaezfektzvesksdwr.supabase.co")
    key = os.environ.get("CMS_SB_KEY") or os.environ.get("CMS_SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        sys.exit("No CMS token: set CMS_TOKEN, or CMS_SB_KEY (service_role of the CMS project).")
    r = requests.get(f"{url}/rest/v1/settings", params={"key": "eq.cms_token", "select": "value"},
                     headers={"apikey": key, "Authorization": "Bearer " + key}, timeout=30)
    r.raise_for_status()
    return r.json()[0]["value"]["access_token"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="B0812001")
    ap.add_argument("--brand", default="76471")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    load_run_env()

    sb_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not a.dry_run and not (sb_url and sb_key):
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

    tok = cms_token()
    h = {"Authorization": "Bearer " + tok, "Accept": "application/json"}

    mfs = requests.get(f"{CMS}/api/express/cms/v1/sku/masterFile/brand",
                       params={"brandId": a.brand, "lang": "zh"}, headers=h, timeout=30).json()
    mfs = mfs.get("data", mfs) if isinstance(mfs, dict) else mfs
    print(f"{len(mfs)} master files for brand {a.brand}", file=sys.stderr)

    def cat(cats, level):
        for c in cats or []:
            if c.get("level") == level:
                return c.get("categoryNameZh")
        return None

    rows = {}
    for mf in mfs:
        mfid = mf["id"]; mfname = mf.get("name")
        page = 1
        while True:
            r = requests.get(f"{CMS}/api/express-order/cms/v2/master-file-sku/simplifySearch",
                             params={"masterFile": mfid, "pageSize": 50, "currentPage": page,
                                     "keyword": "", "lang": "zh"}, headers=h, timeout=40)
            j = r.json()
            data = j.get("data", [])
            pg = j.get("pagination", {})
            for x in data:
                code = str(x.get("skuCode") or "")
                if "_S_" not in code:
                    continue
                suffix = code.split("_S_")[-1]
                img = (x.get("image") or {}).get("url") or ""
                if img.startswith("//"):
                    img = "https:" + img
                cats = x.get("categories")
                rows[suffix] = {
                    "store_code": a.store, "sku_id": suffix, "sku_code": code,
                    "name_zh": x.get("nameZh"), "name_en": x.get("nameEn"),
                    "image_url": img, "cat_l1": cat(cats, 1), "cat_l2": cat(cats, 2),
                    "cat_l3": cat(cats, 3), "master_file": mfname,
                    "active": x.get("active"),
                    "original_price": x.get("originalPrice"),
                    "discount_price": x.get("discountPrice"),
                }
            if page >= pg.get("totalPages", 1):
                break
            page += 1
        print(f"  {mfname}: cumulative {len(rows)} SKUs", file=sys.stderr)

    out = list(rows.values())
    print(f"{len(out)} unique SKUs", file=sys.stderr)
    if a.dry_run:
        print(out[0] if out else "none", file=sys.stderr)
        return

    sb = requests.Session()
    sb.headers.update({"apikey": sb_key, "Authorization": "Bearer " + sb_key,
                       "Content-Type": "application/json",
                       "Prefer": "resolution=merge-duplicates,return=minimal"})
    for i in range(0, len(out), 500):
        resp = sb.post(f"{sb_url}/rest/v1/imax_sku_master", json=out[i:i + 500], timeout=120)
        if resp.status_code >= 300:
            sys.exit(f"write failed {resp.status_code}: {resp.text[:300]}")
    print(f"upserted {len(out)} master rows", file=sys.stderr)


if __name__ == "__main__":
    main()
