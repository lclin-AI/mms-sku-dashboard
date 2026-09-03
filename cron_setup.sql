create or replace function public.dispatch_github_workflow(workflow text)
returns void
language plpgsql
security definer
set search_path = public, vault, net
as $$
declare tok text;
begin
  select decrypted_secret into tok
    from vault.decrypted_secrets where name = 'github_pat_mms';
  if tok is null or length(tok) < 10 then
    raise notice 'github_pat_mms not set in Vault; skipping dispatch';
    return;
  end if;
  perform net.http_post(
    url     := 'https://api.github.com/repos/lclin-AI/mms-sku-dashboard/actions/workflows/'||workflow||'/dispatches',
    headers := jsonb_build_object(
                 'Authorization','Bearer '||tok,
                 'Accept','application/vnd.github+json',
                 'User-Agent','supabase-pg-cron',
                 'Content-Type','application/json'),
    body    := jsonb_build_object('ref','main')
  );
end;
$$;

select cron.unschedule('mms-frequent-dispatch') where exists (select 1 from cron.job where jobname='mms-frequent-dispatch');
select cron.unschedule('mms-backfill-dispatch') where exists (select 1 from cron.job where jobname='mms-backfill-dispatch');

select cron.schedule('mms-frequent-dispatch','*/5 * * * *', $$select public.dispatch_github_workflow('frequent.yml')$$);
select cron.schedule('mms-backfill-dispatch','0 23 * * *',  $$select public.dispatch_github_workflow('daily-backfill.yml')$$);

select jobid, jobname, schedule, active from cron.job order by jobname;
