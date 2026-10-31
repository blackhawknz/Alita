'use client';

import { memo, useState, type ReactNode } from 'react';
import {
  Ban,
  ChevronRight,
  CircleCheck,
  CircleDashed,
  CircleX,
  ShieldAlert,
  ShieldCheck,
} from 'lucide-react';
import { withToken } from '@/lib/api';
import { cx, formatDuration } from '@/lib/format';
import { describeTool } from '@/lib/tools';
import { todosFromResult, type ToolCallView } from '@/lib/turns';
import { Spinner } from '../ui';
import TodoList from './TodoList';

const MAX_PREVIEW = 4000;

function pretty(v: unknown): string {
  if (typeof v === 'string') return v;
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

function Expandable({ text, className }: { text: string; className?: string }) {
  const [full, setFull] = useState(false);
  const long = text.length > MAX_PREVIEW;
  return (
    <div>
      <pre
        className={cx(
          'max-h-[420px] overflow-auto whitespace-pre-wrap break-words rounded-lg border border-line bg-bg/70 px-3 py-2 font-mono text-[11.5px] leading-relaxed text-fg-2',
          className,
        )}
      >
        {long && !full ? `${text.slice(0, MAX_PREVIEW)}\n…` : text}
      </pre>
      {long && (
        <button className="mt-1 text-[11px] font-medium text-primary hover:underline" onClick={() => setFull((f) => !f)}>
          {full ? 'Show less' : `Show more (${Math.round(text.length / 1000)}K chars)`}
        </button>
      )}
    </div>
  );
}

function Label({ children }: { children: ReactNode }) {
  return <div className="mb-1 text-[10.5px] font-semibold uppercase tracking-wider text-muted-2">{children}</div>;
}

/** Pull common output fields out of a tool result for nicer display. */
function splitResult(result: unknown): { error?: string; streams: { label: string; text: string }[]; rest: unknown } {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return { streams: [], rest: result };
  const r = { ...(result as Record<string, unknown>) };
  const streams: { label: string; text: string }[] = [];
  for (const key of ['stdout', 'output', 'stderr', 'content', 'text']) {
    const v = r[key];
    if (typeof v === 'string' && v.trim()) {
      streams.push({ label: key, text: v });
      delete r[key];
    }
  }
  const error = typeof r.error === 'string' ? r.error : undefined;
  if (error) delete r.error;
  return { error, streams, rest: r };
}

function imagesFromResult(result: unknown): { url: string; seed?: unknown }[] {
  if (!result || typeof result !== 'object') return [];
  const imgs = (result as { images?: unknown }).images;
  if (!Array.isArray(imgs)) return [];
  return imgs
    .filter((i): i is Record<string, unknown> => !!i && typeof i === 'object' && typeof (i as Record<string, unknown>).url === 'string')
    .map((i) => ({ url: String(i.url), seed: i.seed }))
    .filter((i) => i.url.startsWith('/api/') || i.url.startsWith('http') || i.url.startsWith('data:image'));
}

/** Audio preview (voice tool) and the chosen voice's name, if any. */
function voiceFromResult(result: unknown): { audio?: string; name?: string } {
  if (!result || typeof result !== 'object') return {};
  const r = result as Record<string, unknown>;
  const audio = typeof r.audio_url === 'string' && /^(\/api\/voice\/|https:\/\/)/.test(r.audio_url) ? r.audio_url : undefined;
  const voice = r.voice;
  const name =
    typeof voice === 'string'
      ? voice
      : voice && typeof voice === 'object' && typeof (voice as Record<string, unknown>).name === 'string'
        ? String((voice as Record<string, unknown>).name)
        : undefined;
  return { audio, name };
}

const SEVERITY: Record<string, string> = {
  critical: 'border-danger/40 bg-danger/[0.08] text-danger',
  high: 'border-danger/35 bg-danger/[0.07] text-danger',
  medium: 'border-warn/35 bg-warn/[0.07] text-warn',
  low: 'border-accent/35 bg-accent/[0.07] text-accent',
};

function ToolCard({
  tool,
  onApprove,
}: {
  tool: ToolCallView;
  onApprove?: (approvalId: string, approved: boolean) => Promise<void> | void;
}) {
  const running = tool.status === 'running' || tool.status === 'awaiting_approval';
  const pres = describeTool(tool.name, tool.params, running);
  const isTodo = tool.name === 'todo';
  const todos = isTodo ? todosFromResult(tool.result) || todosFromResult(tool.params) : null;
  const images = tool.name === 'easy_diffusion' ? imagesFromResult(tool.result) : [];
  const voice = tool.name === 'voice' && tool.status === 'success' ? voiceFromResult(tool.result) : {};
  const [open, setOpen] = useState(false);
  const [resolving, setResolving] = useState(false);
  const Icon = pres.icon;
  const approval = tool.approval;
  const pending = approval?.state === 'pending' && tool.status === 'awaiting_approval';

  const decide = async (ok: boolean) => {
    if (!approval || !onApprove) return;
    setResolving(true);
    try {
      await onApprove(approval.id, ok);
    } finally {
      setResolving(false);
    }
  };

  const split = splitResult(tool.result);
  const hasParams = Object.keys(tool.params || {}).length > 0;

  let status: ReactNode;
  switch (tool.status) {
    case 'running':
      status = <Spinner className="h-3.5 w-3.5 text-primary" />;
      break;
    case 'awaiting_approval':
      status = (
        <span className="flex items-center gap-1 text-[11px] font-medium text-warn">
          <ShieldAlert className="h-3.5 w-3.5" /> Needs approval
        </span>
      );
      break;
    case 'success':
      status = <CircleCheck className="h-3.5 w-3.5 text-teal" aria-label="Succeeded" />;
      break;
    case 'error':
      status = <CircleX className="h-3.5 w-3.5 text-danger" aria-label="Failed" />;
      break;
    case 'denied':
      status = (
        <span className="flex items-center gap-1 text-[11px] text-danger">
          <Ban className="h-3.5 w-3.5" /> Denied
        </span>
      );
      break;
    case 'interrupted':
      status = (
        <span className="flex items-center gap-1 text-[11px] text-muted" title="No result was recorded">
          <CircleDashed className="h-3.5 w-3.5" /> No result
        </span>
      );
      break;
  }

  return (
    <div
      className={cx(
        'overflow-hidden rounded-xl border bg-surface/80 transition-colors',
        pending ? 'border-warn/40' : tool.status === 'error' ? 'border-danger/25' : 'border-line',
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2.5 px-3 py-2 text-left transition-colors hover:bg-white/[0.025]"
      >
        <span
          className="flex h-6 w-6 flex-none items-center justify-center rounded-md border border-line bg-elevated"
          style={{ color: pres.color }}
        >
          <Icon className="h-3.5 w-3.5" />
        </span>
        <span className={cx('flex-none text-[13px] font-medium', running ? 'shimmer-text' : 'text-fg-2')}>{pres.label}</span>
        {pres.detail && <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-muted">{pres.detail}</span>}
        {!pres.detail && <span className="flex-1" />}
        {tool.durationMs != null && !running && (
          <span className="flex-none font-mono text-[10.5px] text-muted-2">{formatDuration(tool.durationMs)}</span>
        )}
        <span className="flex-none">{status}</span>
        <ChevronRight className={cx('h-3.5 w-3.5 flex-none text-muted transition-transform', open && 'rotate-90')} />
      </button>

      {/* Approval gate (always visible while relevant) */}
      {approval && (pending || resolving) && (
        <div className={cx('mx-3 mb-3 rounded-lg border px-3 py-2.5', SEVERITY[approval.severity] || SEVERITY.medium)}>
          <div className="flex items-start gap-2">
            <ShieldAlert className="mt-0.5 h-4 w-4 flex-none" />
            <div className="min-w-0 flex-1">
              <p className="text-[12.5px] font-semibold">
                Approval required{approval.severity ? <span className="font-normal opacity-80"> · {approval.severity} risk</span> : null}
              </p>
              {approval.reason && <p className="mt-0.5 text-[12px] leading-relaxed text-fg-2">{approval.reason}</p>}
              <div className="mt-2.5 flex flex-wrap gap-2">
                <button className="btn-primary px-3 py-1 text-xs" disabled={resolving} onClick={() => decide(true)}>
                  {resolving ? <Spinner className="h-3 w-3" /> : <ShieldCheck className="h-3.5 w-3.5" />}
                  Approve
                </button>
                <button className="btn-danger px-3 py-1 text-xs" disabled={resolving} onClick={() => decide(false)}>
                  <Ban className="h-3.5 w-3.5" />
                  Deny
                </button>
                <button className="btn-ghost px-2 py-1 text-xs" onClick={() => setOpen(true)}>
                  Review details
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      {approval && approval.state !== 'pending' && !open && tool.status === 'denied' && (
        <div className="px-3 pb-2 text-[11.5px] text-danger/90">This action was denied, so it did not run.</div>
      )}

      {/* Inline rich previews */}
      {todos && todos.length > 0 && (
        <div className="border-t border-line px-3 py-2.5">
          <TodoList todos={todos} />
        </div>
      )}
      {images.length > 0 && (
        <div className="flex flex-wrap gap-2 border-t border-line px-3 py-2.5">
          {images.map((img, i) => (
            <a key={i} href={withToken(img.url)} target="_blank" rel="noopener noreferrer" title={img.seed != null ? `Seed ${String(img.seed)}` : 'Open image'}>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={withToken(img.url)}
                alt={`Generated image ${i + 1}`}
                className="h-36 w-36 rounded-lg border border-line object-cover transition-transform hover:scale-[1.02]"
                loading="lazy"
              />
            </a>
          ))}
        </div>
      )}
      {voice.audio && (
        <div className="flex items-center gap-3 border-t border-line px-3 py-2.5">
          {voice.name && <span className="flex-none text-[12px] text-fg-2">{voice.name}</span>}
          <audio controls preload="none" src={withToken(voice.audio)} className="h-8 min-w-0 flex-1" />
        </div>
      )}
      {!voice.audio && voice.name && tool.params?.action === 'set' && (
        <div className="border-t border-line px-3 py-2 text-[12px] text-fg-2">
          Voice: <span className="font-medium text-fg">{voice.name}</span>
        </div>
      )}
      {!open && tool.status === 'error' && split.error && (
        <div className="truncate px-3 pb-2 font-mono text-[11.5px] text-danger/90" title={split.error}>
          {split.error}
        </div>
      )}

      {open && (
        <div className="space-y-3 border-t border-line px-3 py-3 animate-fade-in">
          {hasParams && (
            <div>
              <Label>{tool.name || 'Tool'} · arguments</Label>
              <Expandable text={pretty(tool.params)} />
            </div>
          )}
          {tool.result !== undefined && (
            <div className="space-y-2">
              {split.error && (
                <div>
                  <Label>Error</Label>
                  <Expandable text={split.error} className="border-danger/25 text-danger/90" />
                </div>
              )}
              {split.streams.map((s) => (
                <div key={s.label}>
                  <Label>{s.label}</Label>
                  <Expandable text={s.text} className={s.label === 'stderr' ? 'text-warn/90' : undefined} />
                </div>
              ))}
              {split.rest !== undefined &&
                !(typeof split.rest === 'object' && split.rest !== null && Object.keys(split.rest).length === 0) && (
                  <div>
                    <Label>Result</Label>
                    <Expandable text={pretty(split.rest)} />
                  </div>
                )}
            </div>
          )}
          {tool.result === undefined && !running && <p className="text-xs text-muted">No result recorded (the turn was interrupted).</p>}
          {tool.result === undefined && running && <p className="text-xs text-muted">Waiting for result…</p>}
        </div>
      )}
    </div>
  );
}

export default memo(ToolCard);
