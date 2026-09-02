// Public config. The anon key is safe to publish: this project contains ONLY
// mms_sku_daily, RLS is on, and the single policy is anon SELECT. Verified:
// anon INSERT -> 42501 row-level security violation.
window.DASH_CONFIG = {
  SUPABASE_URL: "https://owdshvgtkikubkphtfww.supabase.co",
  SUPABASE_ANON_KEY: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im93ZHNodmd0a2lrdWJrcGh0Znd3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODgzMjY5MDEsImV4cCI6MjEwMzkwMjkwMX0.tCglQJAexhO0qEXOSqx3XdOG7CXbx7NBuacHxiwsL3g",
  DEFAULT_STORE: "B0812001",
};
