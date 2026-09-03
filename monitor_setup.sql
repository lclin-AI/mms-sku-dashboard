-- Cloud freshness monitor: alerts #mms-sku-dashboard via an Incoming Webhook
-- (Vault secret 'slack_webhook') when mms_sku_daily stops updating.

create table if not exists public.mms_monitor_state (
  id int primary key default 1,
  broken boolean not null default false,
  last_alert timestamptz
);
insert into public.mms_monitor_state (id) values (1) on conflict do nothing;

create or replace function public.mms_freshness_alert()
returns void
language plpgsql
security definer
set search_path = public, vault, net
as $$
declare
  hook   text;
  latest timestamptz;
  age_min numeric;
  st     public.mms_monitor_state%rowtype;
  stale  boolean;
  msg    text;
begin
  select decrypted_secret into hook from vault.decrypted_secrets where name = 'slack_webhook';
  if hook is null or length(hook) < 20 then
    raise notice 'slack_webhook not set in Vault; skipping';
    return;
  end if;

  select max(updated_at) into latest from public.mms_sku_daily;
  age_min := case when latest is null then null
                  else extract(epoch from (now() - latest)) / 60 end;
  stale := latest is null or age_min > 90;   -- frequent load is every ~5 min

  select * into st from public.mms_monitor_state where id = 1;

  if stale then
    if (not st.broken) or st.last_alert is null
       or (now() - st.last_alert) > interval '6 hours' then
      msg := ':rotating_light: MMS SKU dashboard data is STALE — last update '
             || coalesce(to_char(latest,'YYYY-MM-DD HH24:MI')||' UTC ('||round(age_min)||' min ago)', 'never')
             || '. The pg_cron -> GitHub load is likely failing. '
             || 'Check https://github.com/lclin-AI/mms-sku-dashboard/actions';
      perform net.http_post(
        url := hook,
        headers := jsonb_build_object('Content-Type','application/json'),
        body := jsonb_build_object('text', msg));
      update public.mms_monitor_state set broken = true, last_alert = now() where id = 1;
    end if;
  else
    if st.broken then
      perform net.http_post(
        url := hook,
        headers := jsonb_build_object('Content-Type','application/json'),
        body := jsonb_build_object('text',
          ':white_check_mark: MMS SKU dashboard data recovered — last update '
          || to_char(latest,'YYYY-MM-DD HH24:MI') || ' UTC (' || round(age_min) || ' min ago).'));
    end if;
    update public.mms_monitor_state set broken = false where id = 1;
  end if;
end;
$$;

select cron.unschedule('mms-freshness-alert') where exists (select 1 from cron.job where jobname='mms-freshness-alert');
select cron.schedule('mms-freshness-alert','*/30 * * * *', $$select public.mms_freshness_alert()$$);

select jobname, schedule, active from cron.job order by jobname;
