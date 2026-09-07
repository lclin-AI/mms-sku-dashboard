// mms-adjust — server-side MMS inventory manual adjust (Add / Deduct / Set).
//
// The public dashboard calls this with { store_code, sku_id, mode, qty, password }.
// This function:
//   1. checks the shared password (ADJUST_PASSWORD)               -> 401 if wrong
//   2. reads the fresh MMS token from app_settings.mms_adjust_token (service role)
//      - if the token is expired / near-expiry, it triggers the token feeder
//        workflow (if GITHUB_PAT is set) and asks the caller to retry shortly
//   3. resolves the SKU uuid, reads current warehouse stock
//   4. PUTs the adjust to MMS  /inventory/api/v2/product-inventory/warehouse
//   5. re-reads and returns { before, after }
//
// Scope is HARD-LOCKED to one merchant/store so a leak cannot touch other stores.
//
// Secrets (Edge Function): ADJUST_PASSWORD (required),
//   GITHUB_PAT + GITHUB_REPO (optional, enables on-demand token refresh).
// SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are injected by the platform.

const MMS = "https://merchant-web.shoalter.com/inventory/api/v2/product-inventory";
const MERCHANT_ID = 70009;                    // HKTVEXPRESS LIMITED (B0812001)
const ALLOWED_STORES = new Set(["B0812001"]); // blast-radius lock
const MODES = new Set(["add", "deduct", "set"]);

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

// fetch with a hard timeout so a slow/blocked MMS call returns an error instead
// of hanging the request (and the dashboard button) forever.
async function fetchT(url: string, init: RequestInit = {}, ms = 20000) {
  const c = new AbortController();
  const t = setTimeout(() => c.abort(), ms);
  try {
    return await fetch(url, { ...init, signal: c.signal });
  } finally {
    clearTimeout(t);
  }
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...CORS },
  });

function tokenSecondsLeft(tok: string): number {
  try {
    const p = JSON.parse(atob(tok.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    if (!p.exp) return 9999;
    return p.exp - Math.floor(Date.now() / 1000);
  } catch {
    return 9999; // not a JWT we can read; assume usable, MMS will reject if not
  }
}

async function triggerRefresh() {
  const pat = Deno.env.get("GITHUB_PAT");
  const repo = Deno.env.get("GITHUB_REPO"); // e.g. "lclin-AI/mms-sku-dashboard"
  if (!pat || !repo) return false;
  const r = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/mms-adjust-token.yml/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${pat}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "mms-adjust",
      },
      body: JSON.stringify({ ref: "main" }),
    },
  );
  return r.ok;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  let step = "start";
  try {
    let b: any;
    try { b = await req.json(); } catch { return json({ error: "bad json" }, 400); }

    // 1) auth
    const pw = Deno.env.get("ADJUST_PASSWORD");
    if (!pw) return json({ error: "server not configured (ADJUST_PASSWORD)" }, 500);
    if (b.password !== pw) return json({ error: "密碼錯誤" }, 401);

    // 2) validate input
    const store = String(b.store_code || "");
    const sku = String(b.sku_id || "");
    const mode = String(b.mode || "");
    const qty = Number(b.qty);
    if (!ALLOWED_STORES.has(store)) return json({ error: `store ${store} 不允許` }, 400);
    if (!sku) return json({ error: "missing sku_id" }, 400);
    if (!MODES.has(mode)) return json({ error: "mode 必須 add/deduct/set" }, 400);
    if (!Number.isFinite(qty) || qty < 0 || qty > 100000)
      return json({ error: "qty 必須 0–100000" }, 400);

    // 3) token
    step = "read-token";
    const SB = Deno.env.get("SUPABASE_URL");
    const KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
    const sr = await fetchT(
      `${SB}/rest/v1/app_settings?key=eq.mms_adjust_token&select=value,updated_at`,
      { headers: { apikey: KEY!, Authorization: `Bearer ${KEY}` } }, 10000,
    );
    const rows = await sr.json();
    const tok = rows?.[0]?.value;
    if (!tok) {
      await triggerRefresh();
      return json({ error: "token 未就緒,已觸發更新,請 1–2 分鐘後再試" }, 503);
    }
    if (tokenSecondsLeft(tok) < 60) {
      await triggerRefresh();
      return json({ error: "token 過期,已觸發更新,請 1–2 分鐘後再試" }, 503);
    }

    const H = { Authorization: `Bearer ${tok}`, "Content-Type": "application/json" };

    // 4) resolve uuid (scoped to our merchant)
    step = "list-sku";
    const listResp = await fetchT(MMS, {
      method: "POST",
      headers: H,
      body: JSON.stringify({
        pageNumber: 1, pageSize: 50, skuId: sku,
        buCodeList: ["HKTV"], merchantId: MERCHANT_ID,
      }),
    });
    if (listResp.status === 401) {
      await triggerRefresh();
      return json({ error: "token 被拒,已觸發更新,請稍後再試" }, 503);
    }
    const list = await listResp.json();
    const content = (list?.response?.content || []).filter(
      (x: any) => String(x.merchantId) === String(MERCHANT_ID) && String(x.skuId) === sku,
    );
    if (!content.length) return json({ error: `搵唔到 SKU ${sku}` }, 404);
    const uuid = content[0].uuid;

    // 5) detail -> current warehouse stock
    const detail = async () => {
      const d = await (await fetchT(`${MMS}/${uuid}`, { headers: H }, 30000)).json();
      return (d?.response?.buInventoryDetails || []).find(
        (x: any) => String(x.storeId) === store,
      );
    };
    step = "detail";
    const bu = await detail();
    if (!bu) return json({ error: "detail 無此 store 的庫存" }, 404);
    const stocks = bu.stockInfoList || [];
    if (stocks.length !== 1)
      return json({ error: `此 SKU 有 ${stocks.length} 個倉,需人手處理` }, 409);
    const st = stocks[0];
    const before = st.stockQty;

    // read-only preview: return current stock without writing anything
    if (b.action === "get") {
      return json({
        ok: true, action: "get", sku_id: sku, sku_name: content[0].skuNameCh,
        current: before, warehouseSeqNo: st.warehouseSeqNo,
      });
    }

    // 6) PUT the adjust
    step = "put";
    const payload = {
      uuid,
      productReadyMethod: bu.productReadyMethod,
      warehouseList: [{
        warehouseSeqNo: st.warehouseSeqNo,
        storeId: bu.storeId,
        storeSkuId: bu.storeSkuId,
        mode, qty,
      }],
    };
    const put = await fetchT(`${MMS}/warehouse`, {
      method: "PUT", headers: H, body: JSON.stringify(payload),
    }, 30000);
    const putBody = await put.text();
    if (put.status >= 300 || !putBody.includes("SUCCESS"))
      return json({ error: `MMS 拒絕: ${put.status} ${putBody.slice(0, 200)}` }, 502);

    // 7) re-read
    step = "reread";
    const bu2 = await detail();
    const after = bu2?.stockInfoList?.[0]?.stockQty;

    return json({
      ok: true, sku_id: sku, sku_name: content[0].skuNameCh,
      mode, qty, before, after,
      warehouseSeqNo: st.warehouseSeqNo,
    });
  } catch (e) {
    const aborted = e instanceof DOMException && e.name === "AbortError";
    return json({
      error: aborted ? `逾時（step: ${step}）— MMS 回應太慢或連唔到` : `錯誤（step: ${step}）: ${String(e).slice(0, 150)}`,
      step,
    }, aborted ? 504 : 500);
  }
});
