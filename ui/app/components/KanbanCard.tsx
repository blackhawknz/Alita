'use client';

import { useState } from 'react';
import { CircleAlert, CircleCheck, CircleX, Clock, LoaderCircle, Plus, RefreshCw, Trash2, X } from 'lucide-react';
import { cx } from '@/lib/format';
import type { KanbanCard as KanbanCardType } from '@/lib/types';
import { Modal } from './ui';

const STATUS_META: Record<string, { icon: typeof Clock; color: string; label: string }> = {
  idle: { icon: Clock, color: 'text-muted', label: 'pending' },
  running: { icon: LoaderCircle, color: 'text-warn', label: 'executing' },
  succeeded: { icon: CircleCheck, color: 'text-teal', label: 'succeeded' },
  failed: { icon: CircleX, color: 'text-danger', label: 'failed' },
  blocked: { icon: CircleAlert, color: 'text-warn', label: 'blocked' },
};

export default function KanbanCardComponent({
  card,
  onDragStart,
  onDragEnd,
  onDelete,
  onRetry,
  onCancel,
}: {
  card: KanbanCardType;
  onDragStart: (id: string) => void;
  onDragEnd: () => void;
  onDelete: (id: string) => void;
  onRetry: (id: string) => void;
  onCancel: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const meta = STATUS_META[card.status] || STATUS_META.idle;
  const Icon = meta.icon;
  const isRunning = card.status === 'running';
  const tags = card.tags || [];

  return (
    <div
      draggable={!isRunning}
      onDragStart={(e) => {
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', card.id);
        onDragStart(card.id);
      }}
      onDragEnd={onDragEnd}
      onClick={() => setExpanded((v) => !v)}
      onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), setExpanded((v) => !v))}
      role="button"
      tabIndex={0}
      aria-expanded={expanded}
      className={cx(
        'rounded-lg border border-line bg-surface px-3 py-2.5 transition-colors hover:border-line-strong',
        isRunning ? 'cursor-default' : 'cursor-grab active:cursor-grabbing',
      )}
    >
      <div className="flex items-start gap-2">
        <p className="min-w-0 flex-1 truncate text-[13px] font-medium text-fg">{card.title}</p>
        <Icon className={cx('h-3.5 w-3.5 flex-none', meta.color, isRunning && 'animate-spin')} />
      </div>

      <div className="mt-1.5 flex items-center gap-2">
        <span className={cx('font-mono text-[10px] uppercase tracking-wide', meta.color)}>{meta.label}</span>
        {tags.length > 0 && (
          <div className="flex gap-1 overflow-hidden">
            {tags.slice(0, 3).map((tag) => (
              <span key={tag} className="rounded bg-elevated px-1 py-px text-[9.5px] text-muted">
                {tag}
              </span>
            ))}
          </div>
        )}
      </div>

      {card.error && (
        <p className="mt-1.5 truncate text-[10.5px] text-danger" title={card.error}>
          {card.error}
        </p>
      )}

      {expanded && (
        <div className="mt-3 space-y-2 animate-fade-in" onClick={(e) => e.stopPropagation()}>
          <div>
            <span className="text-[9.5px] font-semibold uppercase tracking-wider text-muted-2">Description</span>
            <p className="mt-1 whitespace-pre-wrap text-xs leading-relaxed text-fg-2">{card.description || '(no description)'}</p>
          </div>
          {card.result && Object.keys(card.result).length > 0 && (
            <div>
              <span className="text-[9.5px] font-semibold uppercase tracking-wider text-muted-2">Result</span>
              <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-bg/70 p-2 font-mono text-[10.5px] text-teal">
                {(typeof card.result.response === 'string' ? card.result.response : JSON.stringify(card.result, null, 2)).slice(0, 2000)}
              </pre>
            </div>
          )}
          {(card.executed_at || card.completed_at) && (
            <div className="text-[10px] text-muted-2">
              {card.executed_at && <div>Executed: {card.executed_at}</div>}
              {card.completed_at && <div>Completed: {card.completed_at}</div>}
            </div>
          )}
          <div className="flex gap-1.5 pt-1">
            {card.status === 'failed' && (
              <button className="btn-secondary px-2 py-1 text-[11px]" onClick={() => onRetry(card.id)}>
                <RefreshCw className="h-3 w-3" /> Retry
              </button>
            )}
            {isRunning && (
              <button className="btn-danger px-2 py-1 text-[11px]" onClick={() => onCancel(card.id)}>
                <X className="h-3 w-3" /> Cancel
              </button>
            )}
            {!isRunning && (
              <button className="btn-ghost px-2 py-1 text-[11px] text-danger hover:text-danger" onClick={() => onDelete(card.id)}>
                <Trash2 className="h-3 w-3" /> Delete
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export function CreateCardModal({
  column,
  onClose,
  onCreate,
}: {
  column: string;
  onClose: () => void;
  onCreate: (title: string, description: string, tags: string[]) => void;
}) {
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [tagsStr, setTagsStr] = useState('');

  const submit = () => {
    if (!title.trim()) return;
    const tags = tagsStr
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean);
    onCreate(title.trim(), description, tags);
  };

  return (
    <Modal
      title={`New card · ${column}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-primary" onClick={submit} disabled={!title.trim()}>
            <Plus className="h-3.5 w-3.5" /> Create card
          </button>
        </>
      }
    >
      <div className="space-y-3">
        <div>
          <label className="label" htmlFor="card-title">
            Title
          </label>
          <input
            id="card-title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
            placeholder="Brief title"
            autoFocus
            className="input"
          />
        </div>
        <div>
          <label className="label" htmlFor="card-desc">
            Description (agent task prompt)
          </label>
          <textarea
            id="card-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Full instructions for the agent. This is what runs when the card moves to Executing."
            rows={5}
            className="input resize-y"
          />
        </div>
        <div>
          <label className="label" htmlFor="card-tags">
            Tags (comma-separated)
          </label>
          <input id="card-tags" value={tagsStr} onChange={(e) => setTagsStr(e.target.value)} placeholder="coding, research" className="input" />
        </div>
      </div>
    </Modal>
  );
}
