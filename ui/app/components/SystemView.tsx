'use client';

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { Activity, Ban, Brain, MessagesSquare, Monitor, RefreshCw, Search, ShieldAlert, ShieldCheck, Sparkles, Wrench } from 'lucide-react';
import * as api from '@/lib/api';
import { cx } from '@/lib/format';
import type { Health, PendingApproval, Stats, ToolDetail } from '@/lib/types';
import { useToast } from './Toaster';
import { PageHeader, Spinner } from './ui';

function Stat({ icon, label, value }: { icon: ReactNode; label: string; value: ReactNode }) {
  return (
    <div className="card flex items-center gap-3 px-4 py-3">
      <div className="flex h-9 w-9 items-center justify-center rounded-xl border border-line-strong bg-elevated text-primary">{icon}</div>
      <div>
        <p className="text-lg font-semibold leading-tight text-fg">{value}</p>
        <p className="text-[11.5px] text-muted">{label}</p>
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-1.5 text-[13px]">
      <dt className="text-muted">{label}</dt>
      <dd className="min-w-0 truncate text-right text-fg-2">{children}</dd>
    </div>
  );
}

function Flag({ on, onText, offText }: { on: boolean; onText: string; offText: string }) {
  return (
    <span className={cx('inline-flex items-center gap-1.5', on ? 'text-teal' : 'text-muted')}>
      <span className={cx('h-1.5 w-1.5 rounded-full', on ? 'bg-teal' : 'bg-muted-2')} />
      {on ? onText : offText}
    </span>
  );
}

export default function SystemView({
  health,
  onRefreshHealth,
  onOpenSession,
}: {
  health: Health | null;
  onRefreshHealth: () => Promise<void> | void;
  onOpenSession: (id: string) => void;
}) {
  const toast = useToast();
  const [stats, setStats] = useState<Stats | null>(null);
  const [tools, setTools] = useState<ToolDetail[]>([]);
  const [pending, setPending] = useState<PendingApproval[]>([]);
  const [mode, setMode] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toolQuery, setToolQuery] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [s, t, p] = await Promise.all([api.getStats(), api.getTools(), api.getPendingApprovals(), onRefreshHealth()]);
      setStats(s);
      setTools(t.details || []);
      setPending(p.pending || []);
      setMode(p.mode);
      setError(null);
    } catch (e) {
      setError(api.errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [onRefreshHealth]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    const t = setInterval(() => {
      api
        .getPendingApprovals()
        .then((p) => setPending(p.pending || []))
        .catch(() => {});
    }, 5000);
    return () => clearInterval(t);
  }, []);

  const decide = async (id: string, ok: boolean) => {
    try {
      await api.resolveApproval(id, ok);
      setPending((list) => list.filter((p) => p.approval_id !== id));
      toast.success(ok ? 'Approved' : 'Denied');
    } catch (e) {
      toast.error(api.errorMessage(e));
    }
  };

  const filteredTools = useMemo(() => {
    const q = toolQuery.trim().toLowerCase();
    return tools.filter((t) => !q || t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q));
  }, [tools, toolQuery]);

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="System"
        icon={<Monitor className="h-4 w-4" />}
        subtitle="Agent status, tools and pending approvals"
        actions={
          <button className="btn-secondary" onClick={load} disabled={loading}>
            <RefreshCw className={cx('h-3.5 w-3.5', loading && 'animate-spin')} /> Refresh
          </button>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl space-y-6 px-5 py-6 md:px-8">
          {error && <p className="rounded-lg border border-danger/30 bg-danger/[0.06] px-3 py-2 text-sm text-danger">{error}</p>}

          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat icon={<MessagesSquare className="h-4 w-4" />} label="Chats" value={stats?.sessions ?? '—'} />
            <Stat icon={<Brain className="h-4 w-4" />} label="Memories" value={stats?.memories.total ?? '—'} />
            <Stat icon={<Sparkles className="h-4 w-4" />} label="Skills" value={stats?.skills ?? '—'} />
            <Stat icon={<Wrench className="h-4 w-4" />} label="Tools" value={stats?.tools ?? '—'} />
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div className="card px-4 py-3.5">
              <div className="mb-2 flex items-center gap-2">
                <Activity className={cx('h-4 w-4', health ? 'text-teal' : 'text-danger')} />
                <h2 className="text-sm font-semibold text-fg">Agent</h2>
              </div>
              {health ? (
                <dl className="divide-y divide-line">
                  <Row label="Status">
                    <Flag on={health.status === 'healthy'} onText={health.status} offText={health.status} />
                  </Row>
                  <Row label="Version">{health.version}</Row>
                  <Row label="Provider">{health.llm_provider}</Row>
                  <Row label="Model">
                    <span className="font-mono text-xs">{health.model}</span>
                  </Row>
                  <Row label="Context window">{health.context_window.toLocaleString()} tokens</Row>
                  <Row label="Semantic search">
                    <Flag on={health.vector_store} onText="Available" offText="Unavailable" />
                  </Row>
                  <Row label="Host execution">
                    <Flag on={health.host_exec_allowed} onText="Allowed" offText="Sandboxed only" />
                  </Row>
                  <Row label="Approval mode">{mode || '—'}</Row>
                </dl>
              ) : (
                <p className="text-sm text-muted">{loading ? 'Loading…' : 'Backend unreachable.'}</p>
              )}
            </div>

            <div className="card px-4 py-3.5">
              <div className="mb-2 flex items-center gap-2">
                <Spinner className={cx('h-4 w-4', stats?.running.length ? 'text-primary' : 'hidden')} />
                <h2 className="text-sm font-semibold text-fg">Running now</h2>
              </div>
              {!stats?.running.length ? (
                <p className="py-2 text-[13px] text-muted">Nothing is running.</p>
              ) : (
                <ul className="divide-y divide-line">
                  {stats.running.map((r) => (
                    <li key={r.id} className="flex items-center justify-between gap-3 py-2 text-[13px]">
                      <span className="min-w-0 truncate text-fg-2">{r.title || r.id}</span>
                      <span className="flex flex-none items-center gap-2">
                        <span className="rounded bg-white/5 px-1.5 py-px text-[10.5px] uppercase text-muted">{r.kind}</span>
                        {r.kind === 'chat' && (
                          <button className="text-xs font-medium text-primary hover:underline" onClick={() => onOpenSession(r.id)}>
                            Open
                          </button>
                        )}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          <div className="card px-4 py-3.5">
            <div className="mb-2 flex items-center gap-2">
              <ShieldAlert className={cx('h-4 w-4', pending.length ? 'text-warn' : 'text-muted')} />
              <h2 className="text-sm font-semibold text-fg">Pending approvals</h2>
              {pending.length > 0 && <span className="rounded-full bg-warn/15 px-2 text-[11px] font-semibold text-warn">{pending.length}</span>}
            </div>
            {pending.length === 0 ? (
              <p className="py-2 text-[13px] text-muted">No tool calls are waiting for approval.</p>
            ) : (
              <ul className="space-y-2">
                {pending.map((p) => (
                  <li key={p.approval_id} className="rounded-lg border border-warn/25 bg-warn/[0.04] px-3 py-2.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-[13px] text-fg">{p.tool_name}</span>
                      <span className="rounded bg-warn/15 px-1.5 text-[10.5px] uppercase text-warn">{p.severity}</span>
                      <span className="text-[11px] text-muted-2">{Math.round(p.age_seconds)}s ago</span>
                      <span className="flex-1" />
                      <button className="btn-primary px-2.5 py-1 text-xs" onClick={() => decide(p.approval_id, true)}>
                        <ShieldCheck className="h-3.5 w-3.5" /> Approve
                      </button>
                      <button className="btn-danger px-2.5 py-1 text-xs" onClick={() => decide(p.approval_id, false)}>
                        <Ban className="h-3.5 w-3.5" /> Deny
                      </button>
                    </div>
                    <p className="mt-1 text-xs text-fg-2">{p.reason}</p>
                    <pre className="mt-1.5 max-h-32 overflow-auto rounded bg-bg/70 px-2 py-1 font-mono text-[11px] text-muted">
                      {JSON.stringify(p.params, null, 2)}
                    </pre>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="card px-4 py-3.5">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <Wrench className="h-4 w-4 text-primary" />
                <h2 className="text-sm font-semibold text-fg">Tools ({tools.length})</h2>
              </div>
              <div className="relative w-full sm:w-64">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
                <input value={toolQuery} onChange={(e) => setToolQuery(e.target.value)} placeholder="Filter tools" className="input py-1.5 pl-8 text-[13px]" aria-label="Filter tools" />
              </div>
            </div>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {filteredTools.map((t) => (
                <div key={t.name} className="rounded-lg border border-line bg-bg/60 px-3 py-2.5">
                  <div className="flex items-center gap-1.5">
                    <span className="truncate font-mono text-[12.5px] font-medium text-fg">{t.name}</span>
                  </div>
                  <p className="mt-1 line-clamp-3 text-[11.5px] leading-snug text-muted" title={t.description}>
                    {t.description}
                  </p>
                  {t.tags?.length > 0 && (
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {t.tags.map((tag) => (
                        <span key={tag} className="rounded bg-white/5 px-1.5 py-px text-[10px] text-muted">
                          {tag}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
            {!loading && filteredTools.length === 0 && <p className="py-2 text-[13px] text-muted">No tools match.</p>}
          </div>
        </div>
      </div>
    </div>
  );
}
