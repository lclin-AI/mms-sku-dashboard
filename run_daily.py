"""
Daily unattended runner: log in to MMS, then pull each store's SKU sales into
Supabase. Intended for Windows Task Scheduler.

It gets a fresh accessToken via mms_login.get_token() (which reuses a saved
browser session when possible), sets it as MMS_TOKEN, and invokes
mms_to_supabase for every store in STORES.

Config via env (or run.env next to this file, KEY=VALUE per line):
  SUPABASE_URL                 https://owdshvgtkikubkphtfww.supabase.co
  SUPABASE_SERVICE_ROLE_KEY    service_role key of the dashboard project
  MMS_STORES                   comma list, e.g. B0812001,H0888001  (default B0812001)
  MMS_DAYS                     lookback window, default 12 (MMS retention cap)
  MMS_USERCODE / MMS_PASSWORD  or creds.json (see mms_login.py)

Exit codes: 0 all stores ok, 1 one or more stores failed, 2 login failed.
"""
import os, subprocess, sys, time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env_file():
    p = os.path.join(HERE, "run.env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def main():
    load_env_file()
    stores = [s.strip() for s in os.environ.get("MMS_STORES", "B0812001").split(",") if s.strip()]
    days = os.environ.get("MMS_DAYS", "12")

    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY")):
        log("FATAL: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")
        return 2

    import mms_login
    try:
        log("logging in to MMS...")
        token = mms_login.get_token()
        os.environ["MMS_TOKEN"] = token
        log(f"got accessToken ({len(token)} chars)")
    except SystemExit as e:
        log(f"FATAL login: {e}")
        return 2

    failed = []
    for store in stores:
        log(f"--- {store} (last {days} days) ---")
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "mms_to_supabase.py"),
             "--store", store, "--days", str(days)],
            env=os.environ.copy())
        if r.returncode != 0:
            failed.append(store)
            log(f"{store}: FAILED (exit {r.returncode})")
        else:
            log(f"{store}: ok")
        time.sleep(1)

    if failed:
        log(f"DONE with failures: {', '.join(failed)}")
        return 1
    log(f"DONE ok: {', '.join(stores)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
