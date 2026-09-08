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
  let isWrite = false;
  let failLog: ((err: string) => void) | null = null;
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

    // 4) resolve uuid + current stock via the LIST — FAST (~0.3s). The product
    // detail endpoint is very slow (~30-40s), so we never touch it on the
    // preview and cache its (stable) warehouse structure for writes.
    const SR_H = { apikey: KEY!, Authorization: `Bearer ${KEY}` };
    const curOf = (it: any) => Number(it?.consignmentInventoryQty ?? 0);
    const listOnce = async () => {
      const r = await fetchT(MMS, {
        method: "POST", headers: H,
        body: JSON.stringify({ pageNumber: 1, pageSize: 50, skuId: sku,
          buCodeList: ["HKTV"], merchantId: MERCHANT_ID }),
      }, 20000);
      if (r.status === 401) return { unauth: true } as any;
      const j = await r.json();
      const c = (j?.response?.content || []).filter(
        (x: any) => String(x.merchantId) === String(MERCHANT_ID) && String(x.skuId) === sku);
      return { item: c[0] } as any;
    };
    step = "list-sku";
    const l1 = await listOnce();
    if (l1.unauth) { await triggerRefresh(); return json({ error: "token 被拒,已觸發更新,請稍後再試" }, 503); }
    if (!l1.item) return json({ error: `搵唔到 SKU ${sku}` }, 404);
    const uuid = l1.item.uuid;
    const before = curOf(l1.item);

    // preview: current from the fast list, no slow detail
    if (b.action === "get") {
      return json({ ok: true, action: "get", sku_id: sku, sku_name: l1.item.skuNameCh, current: before });
    }

    // log helper — records BOTH success and failure into adjust_log (best-effort)
    const skuName = l1.item.skuNameCh;
    const operator = typeof b.operator === "string" ? b.operator.slice(0, 60) : null;
    const logAdjust = (status: string, error: string | null, afterV: number | null, wh: string | null) =>
      fetchT(`${SB}/rest/v1/adjust_log`, {
        method: "POST",
        headers: { ...SR_H, "Content-Type": "application/json", Prefer: "return=minimal" },
        body: JSON.stringify({
          store_code: store, sku_id: sku, sku_name: skuName, mode, qty,
          before_qty: before, after_qty: afterV, warehouse: wh, operator,
          status, error: error ? String(error).slice(0, 200) : null,
        }),
      }, 10000).catch(() => {});
    isWrite = true;
    failLog = (err) => logAdjust("fail", err, null, null);

    // 5) write: warehouse structure is stable per SKU — read a cached copy so
    // only the FIRST write of a SKU pays the slow detail call.
    const cacheKey = `struct:${store}:${sku}`;
    let struct: any = null;
    try {
      const cr = await fetchT(`${SB}/rest/v1/app_settings?key=eq.${encodeURIComponent(cacheKey)}&select=value`, { headers: SR_H }, 10000);
      const crows = await cr.json();
      if (crows?.[0]?.value) struct = JSON.parse(crows[0].value);
    } catch (_) { /* fall through to detail */ }

    if (!struct) {
      step = "detail";
      const d = await (await fetchT(`${MMS}/${uuid}`, { headers: H }, 90000)).json();
      const bu = (d?.response?.buInventoryDetails || []).find((x: any) => String(x.storeId) === store);
      if (!bu) return json({ error: "detail 無此 store 的庫存" }, 404);
      const stks = bu.stockInfoList || [];
      if (stks.length !== 1) return json({ error: `此 SKU 有 ${stks.length} 個倉,需人手處理` }, 409);
      struct = { warehouseSeqNo: stks[0].warehouseSeqNo, productReadyMethod: bu.productReadyMethod,
                 storeId: bu.storeId, storeSkuId: bu.storeSkuId };
      // best-effort cache write
      fetchT(`${SB}/rest/v1/app_settings`, { method: "POST",
        headers: { ...SR_H, "Content-Type": "application/json", Prefer: "resolution=merge-duplicates" },
        body: JSON.stringify({ key: cacheKey, value: JSON.stringify(struct) }) }, 10000).catch(() => {});
    }

    // 6) PUT the adjust
    step = "put";
    const payload = {
      uuid,
      productReadyMethod: struct.productReadyMethod,
      warehouseList: [{
        warehouseSeqNo: struct.warehouseSeqNo,
        storeId: struct.storeId,
        storeSkuId: struct.storeSkuId,
        mode, qty,
      }],
    };
    const put = await fetchT(`${MMS}/warehouse`, {
      method: "PUT", headers: H, body: JSON.stringify(payload),
    }, 60000);
    const putBody = await put.text();
    if (put.status >= 300 || !putBody.includes("SUCCESS")) {
      const msg = `MMS 拒絕: ${put.status} ${putBody.slice(0, 150)}`;
      logAdjust("fail", msg, null, struct.warehouseSeqNo);
      return json({ error: msg }, 502);
    }

    // 7) re-read via the fast list for the 'after'
    step = "reread";
    const l2 = await listOnce();
    const after = l2.item ? curOf(l2.item) : null;

    // 8) record the successful adjustment (best-effort)
    logAdjust("success", null, after, struct.warehouseSeqNo);

    return json({
      ok: true, sku_id: sku, sku_name: skuName,
      mode, qty, before, after,
      warehouseSeqNo: struct.warehouseSeqNo,
    });
  } catch (e) {
    const aborted = e instanceof DOMException && e.name === "AbortError";
    const errMsg = aborted ? `逾時（step: ${step}）— MMS 回應太慢或連唔到` : `錯誤（step: ${step}）: ${String(e).slice(0, 150)}`;
    // record write attempts that failed after we knew the SKU/current value
    if (isWrite && failLog) failLog(errMsg);
    return json({ error: errMsg, step }, aborted ? 504 : 500);
  }
});
