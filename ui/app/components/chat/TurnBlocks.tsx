'use client';

import { useState, type ReactNode } from 'react';
import { Archive, Brain, ChevronRight, Lightbulb, Sparkles } from 'lucide-react';
import { cx } from '@/lib/format';
import type { RecallItem, SkillSuggestion } from '@/lib/types';
import Markdown from './Markdown';

export function ReasoningBlock({ text, active }: { text: string; active: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="text-[13px]">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex items-center gap-1.5 rounded-md py-0.5 text-muted transition-colors hover:text-fg-2"
      >
        <Lightbulb className="h-3.5 w-3.5" />
        <span className={cx('font-medium', active && 'shimmer-text')}>{active ? 'Thinking…' : 'Thought'}</span>
        <ChevronRight className={cx('h-3.5 w-3.5 transition-transform', open && 'rotate-90')} />
      </button>
      {open && (
        <div className="mt-1.5 border-l-2 border-line-strong pl-3.5 animate-fade-in">
          <Markdown text={text} className="md-sm text-muted" />
        </div>
      )}
    </div>
  );
}

function Chip({
  icon,
  label,
  children,
}: {
  icon: ReactNode;
  label: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 rounded-full border border-line-strong bg-elevated/70 px-2.5 py-1 text-[11.5px] text-muted transition-colors hover:text-fg-2"
      >
        {icon}
        {label}
        <ChevronRight className={cx('h-3 w-3 transition-transform', open && 'rotate-90')} />
      </button>
      {open && <div className="mt-2 rounded-xl border border-line bg-surface/70 px-3 py-2.5 animate-fade-in">{children}</div>}
    </div>
  );
}

export function RecallChip({ items }: { items: RecallItem[] }) {
  return (
    <Chip icon={<Brain className="h-3.5 w-3.5 text-purple" />} label={`Recalled ${items.length} memor${items.length === 1 ? 'y' : 'ies'}`}>
      <ul className="space-y-1.5">
        {items.map((m) => (
          <li key={m.id} className="flex items-start gap-2 text-[12.5px] leading-snug text-fg-2">
            <span className="mt-px flex-none rounded bg-purple/10 px-1.5 py-px text-[10px] font-medium uppercase tracking-wide text-purple">
              {m.category}
            </span>
            <span>{m.content}</span>
          </li>
        ))}
      </ul>
    </Chip>
  );
}

export function SkillsChip({ skills }: { skills: SkillSuggestion[] }) {
  return (
    <Chip
      icon={<Sparkles className="h-3.5 w-3.5 text-primary" />}
      label={`Relevant skill${skills.length === 1 ? '' : 's'}: ${skills.map((s) => s.name).join(', ')}`}
    >
      <ul className="space-y-1.5">
        {skills.map((s) => (
          <li key={s.name} className="text-[12.5px] leading-snug">
            <span className="font-mono text-primary">{s.name}</span>
            {s.description && <span className="text-muted"> · {s.description}</span>}
          </li>
        ))}
      </ul>
    </Chip>
  );
}

export function CompactionNotice({ status, messages, error }: { status: string; messages?: number; error?: string }) {
  const text =
    status === 'started'
      ? `Compacting context${messages ? ` (${messages} messages)` : ''}…`
      : status === 'done'
        ? `Context compacted${messages ? `: ${messages} earlier messages summarised` : ''}`
        : `Context compaction failed${error ? `: ${error}` : ''}`;
  return <Divider icon={<Archive className="h-3.5 w-3.5" />} text={text} tone={status === 'failed' ? 'error' : 'normal'} active={status === 'started'} />;
}

export function Divider({
  text,
  icon,
  tone = 'normal',
  active,
}: {
  text: string;
  icon?: ReactNode;
  tone?: 'normal' | 'error';
  active?: boolean;
}) {
  return (
    <div className="flex items-center gap-3 py-1 text-[11.5px]" role="separator">
      <div className="h-px flex-1 bg-line-strong" />
      <span className={cx('flex items-center gap-1.5', tone === 'error' ? 'text-danger' : 'text-muted', active && 'shimmer-text')}>
        {icon}
        {text}
      </span>
      <div className="h-px flex-1 bg-line-strong" />
    </div>
  );
}
