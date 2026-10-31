'use client';

import { useCallback, useEffect, useState } from 'react';
import { Calendar, ChevronDown, CircleCheck, CircleX, Clock, LoaderCircle, Play, Plus, RefreshCw, Trash2 } from 'lucide-react';
import * as api from '@/lib/api';
import { cx, formatDuration, parseUtc, shortDateTime } from '@/lib/format';
import type { JobRun, ScheduledJob } from '@/lib/types';
import { useToast } from './Toaster';
import { CenteredSpinner, EmptyState, Modal, PageHeader, Spinner, Toggle } from './ui';

const CRON_PRESETS = [
  { label: 'Every 5 minutes', value: '*/5 * * * *' },
  { label: 'Hourly', value: '0 * * * *' },
  { label: 'Daily 09:00', value: '0 9 * * *' },
  { label: 'Daily 00:00', value: '0 0 * * *' },
  { label: 'Weekdays 08:00', value: '0 8 * * 1-5' },
  { label: 'Mondays 09:00', value: '0 9 * * 1' },
];

const fmt = (ts: string) => (ts ? shortDateTime(parseUtc(ts)) : '—');

const FALLBACK_TIMEZONES = [
  'UTC',
  'Pacific/Auckland',
  'Australia/Sydney',
  'Asia/Tokyo',
  'Asia/Singapore',
  'Europe/London',
  'Europe/Berlin',
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
];

function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

function timezoneOptions(): string[] {
  const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
  const supported = intl.supportedValuesOf?.('timeZone') || FALLBACK_TIMEZONES;
  const current = browserTimezone();
  return supported.includes(current) ? supported : [current, ...supported];
}

function RunRow({ run }: { run: JobRun }) {
  const [open, setOpen] = useState(false);
  const Icon = run.status === 'completed' ? CircleCheck : run.status === 'failed' ? CircleX : LoaderCircle;
  const color = run.status === 'completed' ? 'text-teal' : run.status === 'failed' ? 'text-danger' : 'text-warn';
  return (
    <li className="rounded-lg border border-line bg-bg/50">
      <button className="flex w-full items-start gap-2 px-3 py-2 text-left" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <Icon className={cx('mt-0.5 h-3.5 w-3.5 flex-none', color, run.status === 'running' && 'animate-spin')} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 text-[11px]">
            <span className={cx('font-mono', color)}>{run.status}</span>
            <span className="text-muted-2">
              {fmt(run.started_at)} · {formatDuration(run.duration_ms) || '—'}
            </span>
            {run.tool_calls?.length > 0 && (
              <span className="text-muted">
                · {run.tool_calls.length} tool call{run.tool_calls.length !== 1 ? 's' : ''}
              </span>
            )}
          </div>
          {!open && run.response && <p className="mt-0.5 truncate text-[11.5px] text-fg-2">{run.response}</p>}
          {!open && run.error && <p className="mt-0.5 truncate text-[11.5px] text-danger">{run.error}</p>}
        </div>
      </button>
      {open && (
        <div className="space-y-2 border-t border-line px-3 py-2">
          {run.response && <p className="whitespace-pre-wrap text-[12px] leading-relaxed text-fg-2">{run.response}</p>}
          {run.error && <p className="whitespace-pre-wrap text-[12px] text-danger">{run.error}</p>}
          {run.tool_calls?.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {run.tool_calls.map((t, i) => (
                <span key={i} className={cx('rounded px-1.5 py-px font-mono text-[10.5px]', t.success ? 'bg-teal/10 text-teal' : 'bg-danger/10 text-danger')}>
                  {t.tool_name}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </li>
  );
}

export default function JobsView() {
  const toast = useToast();
  const [jobs, setJobs] = useState<ScheduledJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [expandedJob, setExpandedJob] = useState<string | null>(null);
  const [runs, setRuns] = useState<Record<string, JobRun[]>>({});

  const load = useCallback(() => {
    api
      .getJobs()
      .then((j) => {
        setJobs(j);
        setError(null);
      })
      .catch((e) => setError(api.errorMessage(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  const loadRuns = useCallback(async (jobId: string) => {
    try {
      const r = await api.getJobRuns(jobId);
      setRuns((prev) => ({ ...prev, [jobId]: r }));
    } catch {
      /* ignore */
    }
  }, []);

  // Refresh run history while a run is in progress.
  useEffect(() => {
    if (!expandedJob) return;
    const active = (runs[expandedJob] || []).some((r) => r.status === 'running');
    if (!active) return;
    const t = setInterval(() => {
      loadRuns(expandedJob);
      load();
    }, 3000);
    return () => clearInterval(t);
  }, [expandedJob, runs, loadRuns, load]);

  const toggleExpand = (jobId: string) => {
    if (expandedJob === jobId) setExpandedJob(null);
    else {
      setExpandedJob(jobId);
      loadRuns(jobId);
    }
  };

  const handleDelete = async (job: ScheduledJob) => {
    const ok = await toast.confirm({ title: 'Delete scheduled job?', message: job.name, confirmLabel: 'Delete', danger: true });
    if (!ok) return;
    setJobs((prev) => prev.filter((j) => j.id !== job.id));
    try {
      await api.deleteJob(job.id);
    } catch (e) {
      toast.error(api.errorMessage(e));
      load();
    }
  };

  const handleRunNow = async (job: ScheduledJob) => {
    try {
      await api.runJobNow(job.id);
      toast.success(`Started “${job.name}”`);
      setExpandedJob(job.id);
      setTimeout(() => loadRuns(job.id), 800);
    } catch (e) {
      toast.error(`Run failed: ${api.errorMessage(e)}`);
    }
  };

  const handleToggleEnabled = async (job: ScheduledJob) => {
    setJobs((prev) => prev.map((j) => (j.id === job.id ? { ...j, enabled: !j.enabled } : j)));
    try {
      await api.updateJob(job.id, { enabled: !job.enabled });
      load();
    } catch (e) {
      toast.error(api.errorMessage(e));
      load();
    }
  };

  const handleTimezone = async (job: ScheduledJob, timezone: string) => {
    setJobs((prev) => prev.map((j) => (j.id === job.id ? { ...j, timezone } : j)));
    try {
      await api.updateJob(job.id, { timezone });
      load();
    } catch (e) {
      toast.error(api.errorMessage(e));
      load();
    }
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Jobs"
        icon={<Clock className="h-4 w-4" />}
        subtitle={`${jobs.length} job${jobs.length !== 1 ? 's' : ''} · cron-scheduled agent runs`}
        actions={
          <>
            <button className="btn-primary" onClick={() => setShowCreate(true)}>
              <Plus className="h-4 w-4" /> <span className="hidden sm:inline">New job</span>
            </button>
            <button className="icon-btn" onClick={load} title="Refresh" aria-label="Refresh">
              <RefreshCw className="h-4 w-4" />
            </button>
          </>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl px-5 py-5 md:px-8">
          {loading ? (
            <div className="h-64">
              <CenteredSpinner />
            </div>
          ) : error ? (
            <EmptyState icon={<Calendar className="h-5 w-5" />} title="Could not load jobs">
              {error}
            </EmptyState>
          ) : jobs.length === 0 ? (
            <EmptyState icon={<Calendar className="h-5 w-5" />} title="No scheduled jobs">
              Create one to run Alita on a schedule, e.g. a morning news digest.
            </EmptyState>
          ) : (
            <div className="space-y-3">
              {jobs.map((job) => {
                const open = expandedJob === job.id;
                return (
                  <div key={job.id} className="card">
                    <div className="flex items-center gap-3 px-4 py-3">
                      <Toggle checked={job.enabled} onChange={() => handleToggleEnabled(job)} label={job.enabled ? 'Disable job' : 'Enable job'} />
                      <button className="min-w-0 flex-1 text-left" onClick={() => toggleExpand(job.id)} aria-expanded={open}>
                        <p className={cx('truncate text-[13.5px] font-medium', job.enabled ? 'text-fg' : 'text-muted')}>{job.name}</p>
                        <p className="mt-0.5 truncate text-[11px] text-muted">
                          <code className="font-mono text-fg-2">{job.cron_expr}</code> · {job.timezone || 'host local'} · {job.run_count} run{job.run_count !== 1 ? 's' : ''} · last {fmt(job.last_run_at)}
                          {job.enabled && job.next_run_at ? ` · next ${fmt(job.next_run_at)}` : ''}
                        </p>
                      </button>
                      <button className="icon-btn text-teal hover:text-teal" onClick={() => handleRunNow(job)} title="Run now" aria-label="Run now">
                        <Play className="h-4 w-4" />
                      </button>
                      <button className="icon-btn hover:text-danger" onClick={() => handleDelete(job)} title="Delete job" aria-label="Delete job">
                        <Trash2 className="h-4 w-4" />
                      </button>
                      <button className="icon-btn" onClick={() => toggleExpand(job.id)} title={open ? 'Collapse' : 'Expand'} aria-label={open ? 'Collapse' : 'Expand'}>
                        <ChevronDown className={cx('h-4 w-4 transition-transform', open && 'rotate-180')} />
                      </button>
                    </div>
                    {open && (
                      <div className="space-y-3 border-t border-line px-4 py-3 animate-fade-in">
                        {job.description && <p className="text-xs text-muted">{job.description}</p>}
                        <div>
                          <span className="text-[10.5px] font-semibold uppercase tracking-wider text-muted-2">Prompt</span>
                          <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded-lg bg-bg/70 p-2.5 font-mono text-xs text-fg-2">{job.prompt}</pre>
                        </div>
                        <label className="flex flex-wrap items-center gap-2 text-xs text-muted">
                          <span className="text-[10.5px] font-semibold uppercase tracking-wider text-muted-2">Timezone</span>
                          <select
                            value={job.timezone}
                            onChange={(e) => handleTimezone(job, e.target.value)}
                            className="input max-w-xs py-1 text-xs"
                            aria-label={`Timezone for ${job.name}`}
                          >
                            <option value="">Host local ({browserTimezone()})</option>
                            {timezoneOptions().map((zone) => (
                              <option key={zone} value={zone}>
                                {zone}
                              </option>
                            ))}
                          </select>
                        </label>
                        <div>
                          <span className="text-[10.5px] font-semibold uppercase tracking-wider text-muted-2">Recent runs</span>
                          {!runs[job.id] ? (
                            <Spinner className="mt-2 h-4 w-4 text-muted" />
                          ) : runs[job.id].length === 0 ? (
                            <p className="mt-1 text-xs text-muted">No runs yet.</p>
                          ) : (
                            <ul className="mt-2 space-y-1.5">
                              {runs[job.id].map((run) => (
                                <RunRow key={run.id} run={run} />
                              ))}
                            </ul>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {showCreate && (
        <CreateJobModal
          onClose={() => setShowCreate(false)}
          onCreate={async (name, prompt, cronExpr, description, timezone) => {
            try {
              const job = await api.createJob({ name, prompt, cron_expr: cronExpr, description, timezone });
              setJobs((prev) => [job, ...prev]);
              setShowCreate(false);
              toast.success('Job created');
            } catch (e) {
              toast.error(api.errorMessage(e));
            }
          }}
        />
      )}
    </div>
  );
}

function CreateJobModal({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (name: string, prompt: string, cronExpr: string, description: string, timezone: string) => Promise<void>;
}) {
  const [name, setName] = useState('');
  const [prompt, setPrompt] = useState('');
  const [cronExpr, setCronExpr] = useState('0 9 * * *');
  const [description, setDescription] = useState('');
  const [timezone, setTimezone] = useState(browserTimezone);
  const [saving, setSaving] = useState(false);
  const zones = timezoneOptions();
  const valid = name.trim() && prompt.trim() && cronExpr.trim();

  const submit = async () => {
    if (!valid) return;
    setSaving(true);
    await onCreate(name.trim(), prompt.trim(), cronExpr.trim(), description.trim(), timezone);
    setSaving(false);
  };

  return (
    <Modal
      title="New scheduled job"
      onClose={onClose}
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-primary" onClick={submit} disabled={!valid || saving}>
            {saving ? <Spinner className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />} Create job
          </button>
        </>
      }
    >
      <div className="space-y-3">
        <div>
          <label className="label" htmlFor="job-name">
            Name
          </label>
          <input id="job-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Daily inbox summary" autoFocus className="input" />
        </div>
        <div>
          <label className="label" htmlFor="job-desc">
            Description (optional)
          </label>
          <input id="job-desc" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Summarise overnight emails" className="input" />
        </div>
        <div>
          <label className="label" htmlFor="job-prompt">
            Prompt
          </label>
          <textarea
            id="job-prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Check my inbox and summarise the important emails."
            rows={4}
            className="input resize-y"
          />
        </div>
        <div>
          <label className="label" htmlFor="job-cron">
            Schedule (cron)
          </label>
          <input id="job-cron" value={cronExpr} onChange={(e) => setCronExpr(e.target.value)} className="input font-mono" />
          <div className="mt-2 flex flex-wrap gap-1.5">
            {CRON_PRESETS.map((p) => (
              <button
                key={p.value}
                onClick={() => setCronExpr(p.value)}
                className={cx(
                  'rounded-full border px-2 py-0.5 text-[11px] transition-colors',
                  cronExpr === p.value ? 'border-primary/40 bg-primary/10 text-primary' : 'border-line-strong text-muted hover:text-fg-2',
                )}
              >
                {p.label}
              </button>
            ))}
          </div>
          <p className="hint">minute hour day-of-month month day-of-week.</p>
        </div>
        <div>
          <label className="label" htmlFor="job-timezone">
            Timezone
          </label>
          <select id="job-timezone" value={timezone} onChange={(e) => setTimezone(e.target.value)} className="input">
            {zones.map((zone) => (
              <option key={zone} value={zone}>
                {zone}
              </option>
            ))}
          </select>
          <p className="hint">Defaults to this browser&apos;s timezone. Next runs are shown in your local time.</p>
        </div>
      </div>
    </Modal>
  );
}
