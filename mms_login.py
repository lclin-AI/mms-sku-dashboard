"""
Headless MMS login -> accessToken cookie, for unattended daily runs.

Drives the real merchant.shoalter.com login form with Playwright (Chromium),
the same way the CMS token sync logs in. No reverse-engineering of internal
token endpoints: we submit usercode + password on /zh/login and read the
`accessToken` cookie the app sets, exactly as a human sign-in would.

It reuses a saved browser storage state (mms_state.json) so most runs skip the
password step entirely and just refresh; a full login only happens when the
saved session has expired.

Credentials (never committed, never printed):
  MMS_USERCODE, MMS_PASSWORD   env vars, OR a local creds.json:
      { "usercode": "...", "password": "..." }
  Point CREDS_FILE at it, default ./creds.json (gitignored).

Standalone:
  python mms_login.py            # prints the token to stdout, nothing else
  python mms_login.py --headed   # watch it (use for the first supervised run)

As a module:
  from mms_login import get_token
  token = get_token()
"""
import argparse, json, os, sys, time

LOGIN_URL = "https://merchant.shoalter.com/zh/login"
HOME_URL = "https://merchant.shoalter.com/zh/order-management/orders"
STATE_FILE = os.environ.get("MMS_STATE_FILE",
                            os.path.join(os.path.dirname(__file__), "mms_state.json"))
CREDS_FILE = os.environ.get("MMS_CREDS_FILE",
                            os.path.join(os.path.dirname(__file__), "creds.json"))


def _creds():
    u = os.environ.get("MMS_USERCODE")
    p = os.environ.get("MMS_PASSWORD")
    if u and p:
        return u, p
    if os.path.exists(CREDS_FILE):
        d = json.load(open(CREDS_FILE, encoding="utf-8"))
        return d.get("usercode"), d.get("password")
    return None, None


def _token_from_context(ctx):
    for c in ctx.cookies():
        if c["name"] == "accessToken" and c.get("value"):
            return c["value"]
    return None


def _fill_login(page, usercode, password):
    """Robust against minor DOM changes: try role/placeholder/label in turn."""
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)

    def first(*locators):
        for loc in locators:
            try:
                if loc.count() > 0:
                    return loc.first
            except Exception:
                pass
        return None

    user = first(
        page.get_by_placeholder("Usercode"),
        page.get_by_placeholder("usercode"),
        page.locator('input[type="text"]:visible'),
        page.locator('input:not([type="password"]):visible'),
    )
    pwd = first(
        page.get_by_placeholder("Password"),
        page.locator('input[type="password"]:visible'),
    )
    if not user or not pwd:
        raise RuntimeError("login form fields not found (site DOM changed?)")

    user.click()
    user.fill(usercode)
    pwd.click()
    pwd.fill(password)

    btn = None
    for name in ("Continue", "Login", "Sign in", "登入", "繼續"):
        try:
            b = page.get_by_role("button", name=name)
            if b.count() > 0:
                btn = b.first
                break
        except Exception:
            pass
    if btn:
        btn.click()
    else:
        pwd.press("Enter")


def get_token(headed=False, timeout_s=90):
    from playwright.sync_api import sync_playwright

    usercode, password = _creds()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        ctx_args = {}
        if os.path.exists(STATE_FILE):
            ctx_args["storage_state"] = STATE_FILE
        ctx = browser.new_context(**ctx_args)
        page = ctx.new_page()

        # 1) Fast path: saved session may still be valid.
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            tok = _token_from_context(ctx)
            if tok and "login" not in page.url:
                ctx.storage_state(path=STATE_FILE)
                browser.close()
                return tok
        except Exception:
            pass

        # 2) Full login.
        if not (usercode and password):
            browser.close()
            raise SystemExit(
                "Saved session invalid and no credentials available.\n"
                "Set MMS_USERCODE / MMS_PASSWORD env vars, or create creds.json "
                f"({CREDS_FILE}) with {{\"usercode\":..., \"password\":...}}.")

        _fill_login(page, usercode, password)

        deadline = time.time() + timeout_s
        tok = None
        while time.time() < deadline:
            tok = _token_from_context(ctx)
            if tok:
                break
            page.wait_for_timeout(1000)

        if not tok:
            shot = os.path.join(os.path.dirname(__file__), "login_failed.png")
            try:
                page.screenshot(path=shot, full_page=True)
            except Exception:
                pass
            browser.close()
            raise SystemExit(
                f"Login did not yield an accessToken within {timeout_s}s. "
                f"Screenshot: {shot}. If a captcha/2FA appeared, run once with "
                f"--headed to clear it and seed {STATE_FILE}.")

        ctx.storage_state(path=STATE_FILE)
        browser.close()
        return tok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true",
                    help="show the browser (use for the first supervised run)")
    a = ap.parse_args()
    print(get_token(headed=a.headed))
