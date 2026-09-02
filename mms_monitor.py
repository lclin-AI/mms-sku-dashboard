"""
Freshness monitor for the MMS -> Supabase daily job.

The daily runner is silent on success, so this checks that Supabase actually
received fresh data and alerts to Slack if it didn't. Run it a bit after the
daily job (e.g. Task Scheduler at :30 past the load hour).

Check: newest updated_at in mms_sku_daily must be within STALE_HOURS.
Read is done with the public anon key (read-only), so no secret needed here.

Slack (optional): reuses the bot token already in imax-stock-slack/config.json
unless SLACK_TOKEN / SLACK_CHANNEL are set. If no Slack is configured it just
prints, so the monitor is safe to run anywhere.

Exit: 0 fresh, 1 stale/alerted, 2 could not check.
Flags: --stale-hours N, --always (post even when healthy), --test-alert.
"""
import argparse, json, os, sys, urllib.request, urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STALE_HOURS = 30  # daily job + a wide margin
STATE = os.path.join(HERE, "monitor_state.json")


def cfg(key, default=None):
    v = os.environ.get(key)
    if v:
        return v
    p = os.path.join(HERE, "config.js")
    if os.path.exists(p) and key in ("SUPABASE_URL", "SUPABASE_ANON_KEY"):
        txt = open(p, encoding="utf-8").read()
        import re
        m = re.search(rf'{key}:\s*"([^"]+)"', txt)
        if m:
            return m.group(1)
    return default


def newest_updated_at():
    url = cfg("SUPABASE_URL")
    key = cfg("SUPABASE_ANON_KEY")
    if not (url and key):
        raise RuntimeError("no SUPABASE_URL / SUPABASE_ANON_KEY")
    req = urllib.request.Request(
        f"{url}/rest/v1/mms_sku_daily?select=updated_at&order=updated_at.desc&limit=1",
        headers={"apikey": key, "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=30) as r:
        rows = json.load(r)
    return rows[0]["updated_at"] if rows else None


def slack_creds():
    tok = os.environ.get("SLACK_TOKEN")
    ch = os.environ.get("SLACK_CHANNEL")
    if tok and ch:
        return tok, ch
    p = r"C:\Users\lclin\imax-stock-slack\config.json"
    try:
        d = json.load(open(p, encoding="utf-8"))
        tok = tok or d.get("slack_bot_token") or d.get("bot_token") or d.get("token")
        ch = ch or d.get("channel") or d.get("slack_channel")
    except Exception:
        pass
    return tok, ch


def post_slack(text):
    tok, ch = slack_creds()
    if not (tok and ch):
        print("[no Slack configured] " + text)
        return False
    body = json.dumps({"channel": ch, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=body,
        headers={"Authorization": "Bearer " + tok,
                 "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        ok = json.load(r).get("ok")
    print(("Slack sent" if ok else "Slack FAILED") + ": " + text)
    return bool(ok)


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"broken": False}


def save_state(s):
    json.dump(s, open(STATE, "w", encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-hours", type=float,
                    default=float(os.environ.get("STALE_HOURS", DEFAULT_STALE_HOURS)))
    ap.add_argument("--always", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    a = ap.parse_args()

    if a.test_alert:
        post_slack(":test_tube: MMS SKU dashboard monitor test alert.")
        return 0

    try:
        ts = newest_updated_at()
    except Exception as e:
        post_slack(f":warning: MMS SKU dashboard monitor could not read Supabase: {e}")
        return 2

    now = datetime.now(timezone.utc)
    if ts is None:
        age_h = None
        stale = True
        detail = "table is empty"
    else:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        age_h = (now - dt).total_seconds() / 3600
        stale = age_h > a.stale_hours
        detail = f"newest updated_at {age_h:.1f}h ago"

    st = load_state()
    if stale:
        msg = (f":rotating_light: MMS SKU dashboard data is STALE — {detail} "
               f"(threshold {a.stale_hours:.0f}h). The daily load likely failed; "
               f"check run_daily logs / MMS login.")
        if not st.get("broken") or a.always:
            post_slack(msg)
        st["broken"] = True
        save_state(st)
        print(msg)
        return 1

    if st.get("broken"):
        post_slack(f":white_check_mark: MMS SKU dashboard data recovered — {detail}.")
    elif a.always:
        post_slack(f":white_check_mark: MMS SKU dashboard fresh — {detail}.")
    st["broken"] = False
    save_state(st)
    print(f"fresh — {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
