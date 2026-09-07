"""
MMS manual-adjust token feeder.

The manual-adjust Edge Function needs a valid MMS Bearer token, but a Supabase
Edge Function (Deno) cannot run Playwright to log in. So this small job logs in
as the "adjust" account (a browser login, same as mms_login) and stores the
fresh `accessToken` into Supabase `app_settings` (key = mms_adjust_token). The
Edge Function reads it server-side.

Runs in GitHub Actions on a short schedule (the token lives ~30 min). Uses its
OWN session/state file so it never touches the pycheung session used by the
sales sync.

Credentials (never committed):
  MMS_ADJ_USERCODE / MMS_ADJ_PASSWORD   (GitHub Secrets), OR
  a local creds_adjust.json { "usercode": ..., "password": ... }

Env:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

Run:
  python mms_adjust_token_feeder.py
"""
import os, sys, json
from datetime import datetime, timezone

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

    # Map the adjust-account creds onto the names mms_login expects, and give it a
    # dedicated state file so the sales-sync session is untouched.
    if os.environ.get("MMS_ADJ_USERCODE"):
        os.environ["MMS_USERCODE"] = os.environ["MMS_ADJ_USERCODE"]
    if os.environ.get("MMS_ADJ_PASSWORD"):
        os.environ["MMS_PASSWORD"] = os.environ["MMS_ADJ_PASSWORD"]
    os.environ.setdefault("MMS_CREDS_FILE", os.path.join(HERE, "creds_adjust.json"))
    os.environ.setdefault("MMS_STATE_FILE", os.path.join(HERE, "mms_adjust_state.json"))

    import mms_login, requests

    sb_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not (sb_url and sb_key):
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

    print("minting MMS adjust token (headless login)...", flush=True)
    tok = mms_login.get_token()
    if not tok:
        sys.exit("no token")
    print(f"token minted (len={len(tok)})", flush=True)

    row = {"key": "mms_adjust_token", "value": tok,
           "updated_at": datetime.now(timezone.utc).isoformat()}
    r = requests.post(
        f"{sb_url}/rest/v1/app_settings",
        headers={"apikey": sb_key, "Authorization": "Bearer " + sb_key,
                 "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=minimal"},
        data=json.dumps(row), timeout=60)
    if r.status_code >= 300:
        sys.exit(f"Supabase write failed {r.status_code}: {r.text[:300]}")
    print("stored token in app_settings.mms_adjust_token", flush=True)


if __name__ == "__main__":
    main()
