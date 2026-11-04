'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  CircleCheck,
  CircleMinus,
  CircleX,
  Clock,
  DollarSign,
  LoaderCircle,
  RefreshCw,
} from 'lucide-react';
import * as api from '@/lib/api';
import { cx, formatDuration, parseUtc, relativeTime } from '@/lib/format';
import type { ActivityAttention, ActivityResponse, ActivityRun } from '@/lib/types';
import { CenteredSpinner, EmptyState, PageHeader } from './ui';

const money = (n: number, digits = 4) => `$${(n || 0).toFixed(digits)}`;

function statusStyle(status: ActivityRun['status']) {
  if (status === 'succeeded') return { Icon: CircleCheck, color: 'text-teal' };
  if (status === 'failed') return { Icon: CircleX, color: 'text-danger' };
  if (status === 'cancelled') return { Icon: CircleMinus, color: 'text-muted' };
  return { Icon: LoaderCircle, color: 'text-warn' };
}

function StatusIcon({ status, className }: { status: ActivityRun['status']; className?: string }) {
  const { Icon, color } = statusStyle(status);
  return <Icon className={cx('h-3.5 w-3.5 flex-none', color, status === 'running' && 'animate-spin', className)} />;
}

function RunMeta({ run, elapsedMs }: { run: ActivityRun; elapsedMs: number }) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted-2">
      <span className="font-mono text-fg-2">{run.executor}</span>
      <span>· {formatDuration(elapsedMs) || '0ms'}</span>
      <span>· {run.tool_calls} tool call{run.tool_calls !== 1 ? 's' : ''}</span>
      <span>· {money(run.cost)}</span>
      {run.last_activity && <span className="truncate">· {run.last_activity}</span>}
    </div>
  );
}

function RunningRow({ run, extraMs }: { run: ActivityRun; extraMs: number }) {
  return (
    <li className="rounded-lg border border-line bg-bg/50 px-3 py-2">
      <div className="flex items-start gap-2">
        <StatusIcon status="running" className="mt-0.5" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[12.5px] text-fg">{run.task_summary || '(no task summary)'}</p>
          <RunMeta run={run} elapsedMs={run.elapsed_ms + extraMs} />
          <p className="mt-0.5 truncate font-mono text-[10.5px] text-muted-2" title={run.workdir}>
            {run.workdir}
          </p>
        </div>
      </div>
    </li>
  );
}

function RecentRow({ run }: { run: ActivityRun }) {
  const { color } = statusStyle(run.status);
  const outcome = run.error || run.result_summary || '';
  return (
    <li className="rounded-lg border border-line bg-bg/50 px-3 py-2">
      <div className="flex items-start gap-2">
        <StatusIcon status={run.status} className="mt-0.5" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 text-[11px]">
            <span className={cx('font-mono', color)}>{run.status}</span>
            <span className="text-muted-2">
              {relativeTime(parseUtc(run.finished_at || run.started_at))} · {formatDuration(run.duration_ms)}
            </span>
            <span className="text-muted-2">· {money(run.cost)}</span>
            {run.executor && <span className="font-mono text-muted-2">· {run.executor}</span>}
          </div>
          <p className="mt-0.5 truncate text-[12.5px] text-fg">{run.task_summary || '(no task summary)'}</p>
          {outcome && (
            <p className={cx('mt-0.5 truncate text-[11.5px]', run.error ? 'text-danger' : 'text-fg-2')}>{outcome}</p>
          )}
        </div>
      </div>
    </li>
  );
}

function AttentionRow({ item }: { item: ActivityAttention }) {
  if (item.type === 'approval') {
    return (
      <li className="rounded-lg border border-warn/30 bg-warn/5 px-3 py-2">
        <div className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none text-warn" />
          <div className="min-w-0">
            <p className="text-[12px] text-fg">
              Approval needed: <span className="font-mono">{item.tool_name}</span>
            </p>
            <p className="truncate text-[11px] text-muted">{item.reason || item.severity}</p>
          </div>
        </div>
      </li>
    );
  }
  return (
    <li className="rounded-lg border border-danger/30 bg-danger/5 px-3 py-2">
      <div className="flex items-start gap-2">
        <StatusIcon status={item.status} className="mt-0.5" />
        <div className="min-w-0">
          <p className="truncate text-[12px] text-fg">{item.task_summary || '(no task summary)'}</p>
          <p className="truncate text-[11px] text-danger">
            {item.error || `${item.status} · ${formatDuration(item.duration_ms)}`}
          </p>
        </div>
      </div>
    </li>
  );
}

function SpendCard({ data }: { data: ActivityResponse['spend'] }) {
  const cap = data.executor_daily_cap;
  const pct = cap > 0 ? Math.min(100, (data.executor_today / cap) * 100) : 0;
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div>
          <p className="text-[10.5px] font-semibold uppercase tracking-wider text-muted-2">Today</p>
          <p className="mt-0.5 text-[15px] font-semibold text-fg">{money(data.today)}</p>
        </div>
        <div>
          <p className="text-[10.5px] font-semibold uppercase tracking-wider text-muted-2">Month to date</p>
          <p className="mt-0.5 text-[15px] font-semibold text-fg">{money(data.month_to_date)}</p>
        </div>
        <div>
          <p className="text-[10.5px] font-semibold uppercase tracking-wider text-muted-2">Projected month</p>
          <p className="mt-0.5 text-[15px] font-semibold text-fg">{money(data.projected_month)}</p>
        </div>
        <div>
          <p className="text-[10.5px] font-semibold uppercase tracking-wider text-muted-2">External today</p>
          <p className="mt-0.5 text-[15px] font-semibold text-fg">{money(data.executor_today)}</p>
        </div>
      </div>
      <div>
        <div className="flex items-center justify-between text-[11px] text-muted">
          <span>
            External executor spend today
            {cap > 0 ? ` · ${money(data.executor_today)} of ${money(cap, 2)} daily` : ''}
          </span>
          {data.executor_per_run_cap > 0 && <span>Per-run cap {money(data.executor_per_run_cap, 2)}</span>}
        </div>
        {cap > 0 && (
          <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-white/10">
            <div
              className={cx('h-full rounded-full transition-all', pct >= 90 ? 'bg-danger' : pct >= 60 ? 'bg-warn' : 'bg-teal')}
              style={{ width: `${pct}%` }}
            />
          </div>
        )}
      </div>
    </div>
  );
}

export default function ActivityView() {
  const [data, setData] = useState<ActivityResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadedAt, setLoadedAt] = useState(Date.now());
  const [nowTick, setNowTick] = useState(Date.now());

  const load = useCallback(async () => {
    try {
      const next = await api.getActivity();
      setData(next);
      setLoadedAt(Date.now());
      setError(null);
    } catch (e) {
      setError(api.errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const poll = setInterval(load, 5000);
    return () => clearInterval(poll);
  }, [load]);

  useEffect(() => {
    const tick = setInterval(() => setNowTick(Date.now()), 1000);
    return () => clearInterval(tick);
  }, []);

  const extraMs = Math.max(0, nowTick - loadedAt);
  const counts = data?.counts;

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Activity"
        icon={<Activity className="h-4 w-4" />}
        subtitle={
          counts
            ? `${counts.running} running · ${counts.failed_24h} failed in 24h · ${counts.pending_approvals} pending approval${counts.pending_approvals !== 1 ? 's' : ''}`
            : 'what Alita is doing right now'
        }
        actions={
          <button className="icon-btn" onClick={load} title="Refresh" aria-label="Refresh">
            <RefreshCw className="h-4 w-4" />
          </button>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl space-y-4 px-5 py-5 md:px-8">
          {loading ? (
            <div className="h-64">
              <CenteredSpinner />
            </div>
          ) : error && !data ? (
            <EmptyState icon={<Activity className="h-5 w-5" />} title="Could not load activity">
              {error}
            </EmptyState>
          ) : data ? (
            <>
              {data.needs_attention.length > 0 && (
                <section className="card overflow-hidden">
                  <div className="flex items-center gap-2 border-b border-line px-4 py-3">
                    <AlertTriangle className="h-4 w-4 text-warn" />
                    <h2 className="text-sm font-semibold text-fg">Needs attention</h2>
                  </div>
                  <ul className="space-y-1.5 px-4 py-3">
                    {data.needs_attention.map((item, index) => (
                      <AttentionRow key={item.type === 'approval' ? item.approval_id : `${item.id}-${index}`} item={item} />
                    ))}
                  </ul>
                </section>
              )}

              <section className="card overflow-hidden">
                <div className="flex items-center gap-2 border-b border-line px-4 py-3">
                  <LoaderCircle className={cx('h-4 w-4', data.running.length > 0 ? 'animate-spin text-warn' : 'text-muted')} />
                  <h2 className="text-sm font-semibold text-fg">Running now</h2>
                </div>
                {data.running.length === 0 ? (
                  <p className="px-4 py-4 text-[12.5px] text-muted">Nothing running</p>
                ) : (
                  <ul className="space-y-1.5 px-4 py-3">
                    {data.running.map((run) => (
                      <RunningRow key={run.id} run={run} extraMs={extraMs} />
                    ))}
                  </ul>
                )}
              </section>

              <section className="card overflow-hidden">
                <div className="flex items-center gap-2 border-b border-line px-4 py-3">
                  <DollarSign className="h-4 w-4 text-primary" />
                  <h2 className="text-sm font-semibold text-fg">Spend</h2>
                </div>
                <div className="px-4 py-3">
                  <SpendCard data={data.spend} />
                </div>
              </section>

              <section className="card overflow-hidden">
                <div className="flex items-center gap-2 border-b border-line px-4 py-3">
                  <Clock className="h-4 w-4 text-muted" />
                  <h2 className="text-sm font-semibold text-fg">Recent</h2>
                </div>
                {data.recent.length === 0 ? (
                  <p className="px-4 py-4 text-[12.5px] text-muted">No finished delegated runs yet.</p>
                ) : (
                  <ul className="space-y-1.5 px-4 py-3">
                    {data.recent.map((run) => (
                      <RecentRow key={run.id} run={run} />
                    ))}
                  </ul>
                )}
              </section>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
