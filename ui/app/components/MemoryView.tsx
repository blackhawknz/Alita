'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Brain, Check, NotebookPen, Pencil, Plus, RotateCcw, Save, Search, Trash2, X } from 'lucide-react';
import * as api from '@/lib/api';
import { cx, parseUtc, relativeTime } from '@/lib/format';
import { MEMORY_CATEGORIES, type MemoryBlock, type MemoryEntry, type MemoryStats } from '@/lib/types';
import { useToast } from './Toaster';
import { CenteredSpinner, EmptyState, PageHeader, Spinner } from './ui';

const PAGE = 50;

const CATEGORY_COLORS: Record<string, string> = {
  fact: 'text-accent bg-accent/10',
  preference: 'text-purple bg-purple/10',
  person: 'text-teal bg-teal/10',
  project: 'text-primary bg-primary/10',
  event: 'text-warn bg-warn/10',
  procedure: 'text-cyan bg-cyan/10',
  other: 'text-muted bg-white/5',
};

function Importance({ value, onChange, size = 'sm' }: { value: number; onChange?: (v: number) => void; size?: 'sm' | 'md' }) {
  return (
    <div className="flex items-center gap-0.5" role={onChange ? 'radiogroup' : undefined} aria-label={`Importance ${value} of 5`} title={`Importance ${value}/5`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <button
          key={i}
          type="button"
          disabled={!onChange}
          onClick={() => onChange?.(i)}
          aria-label={`Importance ${i}`}
          className={cx(
            'rounded-full transition-colors disabled:cursor-default',
            size === 'md' ? 'h-2.5 w-2.5' : 'h-1.5 w-1.5',
            i <= value ? 'bg-warn' : 'bg-white/15',
            onChange && 'hover:bg-warn/70',
          )}
        />
      ))}
    </div>
  );
}

function CategoryBadge({ category }: { category: string }) {
  return (
    <span className={cx('rounded px-1.5 py-px text-[10px] font-semibold uppercase tracking-wide', CATEGORY_COLORS[category] || CATEGORY_COLORS.other)}>
      {category}
    </span>
  );
}

// ─── Core blocks ───────────────────────────────────────────────────

function BlockEditor({ block, onSaved }: { block: MemoryBlock; onSaved: (b: MemoryBlock) => void }) {
  const toast = useToast();
  const [value, setValue] = useState(block.content);
  const [saving, setSaving] = useState(false);
  useEffect(() => setValue(block.content), [block.content]);
  const dirty = value !== block.content;
  const len = value.trim().length;
  const over = len > block.limit;
  const pct = Math.min(100, (len / block.limit) * 100);

  const save = async () => {
    setSaving(true);
    try {
      const res = await api.updateMemoryBlock(block.name, value);
      onSaved({ ...block, content: value.trim(), chars: res.chars });
      toast.success(`${block.label} saved`);
    } catch (e) {
      toast.error(api.errorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card flex flex-col">
      <div className="border-b border-line px-4 py-3">
        <div className="flex items-center gap-2">
          <NotebookPen className="h-4 w-4 text-primary" />
          <h3 className="text-sm font-semibold text-fg">{block.label}</h3>
          <span className="font-mono text-[10.5px] text-muted-2">{block.name}</span>
        </div>
        <p className="mt-1 text-xs leading-relaxed text-muted">{block.description}</p>
      </div>
      <textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === 's') {
            e.preventDefault();
            if (dirty && !over) save();
          }
        }}
        rows={10}
        placeholder="(empty)"
        aria-label={block.label}
        className="min-h-[200px] flex-1 resize-y bg-transparent px-4 py-3 font-mono text-[12.5px] leading-relaxed text-fg-2 outline-none placeholder:text-muted-2"
      />
      <div className="flex items-center gap-3 border-t border-line px-4 py-2.5">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <div className="h-1 w-24 overflow-hidden rounded-full bg-white/[0.07]">
            <div className={cx('h-full rounded-full', over ? 'bg-danger' : pct > 85 ? 'bg-warn' : 'bg-teal')} style={{ width: `${pct}%` }} />
          </div>
          <span className={cx('font-mono text-[11px]', over ? 'text-danger' : 'text-muted')}>
            {len.toLocaleString()} / {block.limit.toLocaleString()}
          </span>
        </div>
        <button className="btn-ghost px-2 py-1 text-xs" disabled={!dirty || saving} onClick={() => setValue(block.content)} title="Revert changes">
          <RotateCcw className="h-3.5 w-3.5" /> Revert
        </button>
        <button className="btn-primary px-3 py-1 text-xs" disabled={!dirty || saving || over} onClick={save} title={over ? 'Over the character limit' : 'Save (Ctrl+S)'}>
          {saving ? <Spinner className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />} Save
        </button>
      </div>
    </div>
  );
}

// ─── Long-term entries ─────────────────────────────────────────────

function MemoryRow({
  entry,
  onChange,
  onDelete,
}: {
  entry: MemoryEntry;
  onChange: (id: number, patch: Partial<MemoryEntry>) => Promise<boolean>;
  onDelete: (entry: MemoryEntry) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(entry.content);
  const [category, setCategory] = useState(entry.category);
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setSaving(true);
    const ok = await onChange(entry.id, { content: content.trim(), category });
    setSaving(false);
    if (ok) setEditing(false);
  };

  return (
    <li className="group card px-4 py-3 transition-colors hover:border-line-strong">
      <div className="flex items-center gap-2">
        <CategoryBadge category={entry.category} />
        <Importance value={entry.importance} onChange={(v) => onChange(entry.id, { importance: v })} />
        {typeof entry.relevance === 'number' && (
          <span className="font-mono text-[10.5px] text-muted-2" title="Search relevance">
            {Math.round(entry.relevance * 100)}% match
          </span>
        )}
        <span className="ml-auto text-[10.5px] text-muted-2" title={entry.created_at}>
          #{entry.id} · {relativeTime(parseUtc(entry.updated_at || entry.created_at))}
          {entry.access_count > 0 && ` · recalled ${entry.access_count}x`}
        </span>
        {!editing && (
          <div className="flex gap-0.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
            <button className="icon-btn h-7 w-7" onClick={() => setEditing(true)} title="Edit memory" aria-label="Edit memory">
              <Pencil className="h-3.5 w-3.5" />
            </button>
            <button className="icon-btn h-7 w-7 hover:text-danger" onClick={() => onDelete(entry)} title="Delete memory" aria-label="Delete memory">
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
      </div>
      {editing ? (
        <div className="mt-2 space-y-2">
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            onKeyDown={(e) => e.key === 'Escape' && setEditing(false)}
            rows={3}
            autoFocus
            className="input resize-y text-[13px]"
            aria-label="Memory content"
          />
          <div className="flex items-center gap-2">
            <select value={category} onChange={(e) => setCategory(e.target.value)} className="input w-auto py-1 text-xs" aria-label="Category">
              {MEMORY_CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
            <div className="flex-1" />
            <button
              className="btn-ghost px-2.5 py-1 text-xs"
              onClick={() => {
                setEditing(false);
                setContent(entry.content);
                setCategory(entry.category);
              }}
            >
              <X className="h-3.5 w-3.5" /> Cancel
            </button>
            <button className="btn-primary px-3 py-1 text-xs" onClick={save} disabled={saving || !content.trim()}>
              {saving ? <Spinner className="h-3.5 w-3.5" /> : <Check className="h-3.5 w-3.5" />} Save
            </button>
          </div>
        </div>
      ) : (
        <p className="mt-1.5 whitespace-pre-wrap break-words text-[13.5px] leading-relaxed text-fg-2">{entry.content}</p>
      )}
    </li>
  );
}

function AddMemoryForm({ onCreated, onCancel }: { onCreated: () => void; onCancel: () => void }) {
  const toast = useToast();
  const [content, setContent] = useState('');
  const [category, setCategory] = useState<string>('fact');
  const [importance, setImportance] = useState(3);
  const [saving, setSaving] = useState(false);

  const submit = async () => {
    if (!content.trim()) return;
    setSaving(true);
    try {
      const res = await api.createMemory({ content: content.trim(), category, importance });
      toast.success(
        res.status === 'duplicate'
          ? 'Already remembered (duplicate)'
          : res.status === 'merged'
            ? 'Merged with an existing memory'
            : 'Memory saved',
      );
      onCreated();
    } catch (e) {
      toast.error(api.errorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card mb-4 space-y-3 border-primary/30 p-4 animate-fade-in">
      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        rows={3}
        autoFocus
        placeholder="Something Alita should remember, e.g. “Prefers concise answers with code examples.”"
        className="input resize-y"
        aria-label="New memory"
        onKeyDown={(e) => {
          if (e.key === 'Escape') onCancel();
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submit();
        }}
      />
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-xs text-muted">
          Category
          <select value={category} onChange={(e) => setCategory(e.target.value)} className="input w-auto py-1 text-xs">
            {MEMORY_CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <div className="flex items-center gap-2 text-xs text-muted">
          Importance <Importance value={importance} onChange={setImportance} size="md" />
        </div>
        <div className="flex-1" />
        <button className="btn-ghost px-2.5 py-1 text-xs" onClick={onCancel}>
          Cancel
        </button>
        <button className="btn-primary px-3 py-1 text-xs" onClick={submit} disabled={saving || !content.trim()}>
          {saving ? <Spinner className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />} Add memory
        </button>
      </div>
    </div>
  );
}

export default function MemoryView() {
  const toast = useToast();
  const [blocks, setBlocks] = useState<MemoryBlock[] | null>(null);
  const [stats, setStats] = useState<MemoryStats | null>(null);
  const [entries, setEntries] = useState<MemoryEntry[]>([]);
  const [entriesLoading, setEntriesLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [q, setQ] = useState('');
  const [category, setCategory] = useState('');
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reqId = useRef(0);

  const loadOverview = useCallback(async () => {
    try {
      const data = await api.getMemory();
      setBlocks(data.blocks);
      setStats(data.stats);
      setError(null);
    } catch (e) {
      setError(api.errorMessage(e));
    }
  }, []);

  const loadEntries = useCallback(
    async (append = false) => {
      const id = ++reqId.current;
      setEntriesLoading(true);
      try {
        const offset = append ? entries.length : 0;
        const data = await api.listMemories({ q: q.trim(), category, limit: PAGE, offset: q.trim() ? 0 : offset });
        if (id !== reqId.current) return;
        setEntries((prev) => (append ? [...prev, ...data.memories] : data.memories));
        setHasMore(!q.trim() && data.memories.length === PAGE);
      } catch (e) {
        if (id === reqId.current) toast.error(api.errorMessage(e));
      } finally {
        if (id === reqId.current) setEntriesLoading(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [q, category, entries.length],
  );

  useEffect(() => {
    loadOverview();
  }, [loadOverview]);

  useEffect(() => {
    const t = setTimeout(() => loadEntries(false), q ? 300 : 0);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, category]);

  const changeEntry = async (id: number, patch: Partial<MemoryEntry>) => {
    const prev = entries;
    setEntries((list) => list.map((m) => (m.id === id ? { ...m, ...patch } : m)));
    try {
      await api.updateMemory(id, { content: patch.content, category: patch.category, importance: patch.importance });
      if (patch.category) loadOverview();
      return true;
    } catch (e) {
      setEntries(prev);
      toast.error(api.errorMessage(e));
      return false;
    }
  };

  const removeEntry = async (entry: MemoryEntry) => {
    const ok = await toast.confirm({
      title: 'Delete memory?',
      message: <span className="line-clamp-4">{entry.content}</span>,
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteMemory(entry.id);
      setEntries((list) => list.filter((m) => m.id !== entry.id));
      loadOverview();
    } catch (e) {
      toast.error(api.errorMessage(e));
    }
  };

  if (error && !blocks) {
    return (
      <div className="flex h-full flex-col">
        <PageHeader title="Memory" icon={<Brain className="h-4 w-4" />} />
        <EmptyState icon={<Brain className="h-5 w-5" />} title="Could not load memory">
          {error}
        </EmptyState>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Memory"
        icon={<Brain className="h-4 w-4" />}
        subtitle={
          stats
            ? `${stats.total} long-term memor${stats.total === 1 ? 'y' : 'ies'} · semantic search ${stats.semantic_search ? 'on' : 'off (keyword only)'}`
            : 'Loading…'
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl space-y-8 px-5 py-6 md:px-8">
          <section>
            <h2 className="text-sm font-semibold text-fg">Core memory</h2>
            <p className="mb-3 mt-0.5 text-xs text-muted">Always in Alita’s context. Alita edits these herself; you can too.</p>
            {!blocks ? (
              <div className="h-40">
                <CenteredSpinner />
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                {blocks.map((b) => (
                  <BlockEditor key={b.name} block={b} onSaved={(nb) => setBlocks((list) => list?.map((x) => (x.name === nb.name ? nb : x)) || null)} />
                ))}
              </div>
            )}
          </section>

          <section>
            <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold text-fg">Long-term memories</h2>
                <p className="mt-0.5 text-xs text-muted">Recalled automatically when relevant to a conversation.</p>
              </div>
              <button className="btn-primary" onClick={() => setAdding(true)} disabled={adding}>
                <Plus className="h-4 w-4" /> Add memory
              </button>
            </div>

            {adding && (
              <AddMemoryForm
                onCancel={() => setAdding(false)}
                onCreated={() => {
                  setAdding(false);
                  loadEntries(false);
                  loadOverview();
                }}
              />
            )}

            <div className="mb-3 flex flex-col gap-3 md:flex-row md:items-center">
              <div className="relative md:w-80">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder={stats?.semantic_search ? 'Search memories (semantic)' : 'Search memories'}
                  aria-label="Search memories"
                  className="input pl-9"
                />
                {q && (
                  <button className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-fg" onClick={() => setQ('')} aria-label="Clear search" title="Clear">
                    <X className="h-4 w-4" />
                  </button>
                )}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {['', ...MEMORY_CATEGORIES].map((c) => {
                  const count = c ? stats?.by_category?.[c] || 0 : stats?.total || 0;
                  const active = category === c;
                  return (
                    <button
                      key={c || 'all'}
                      onClick={() => setCategory(c)}
                      className={cx(
                        'rounded-full border px-2.5 py-1 text-[11.5px] font-medium capitalize transition-colors',
                        active ? 'border-primary/40 bg-primary/10 text-primary' : 'border-line-strong text-muted hover:text-fg-2',
                      )}
                    >
                      {c || 'All'} <span className="opacity-60">{count}</span>
                    </button>
                  );
                })}
              </div>
            </div>

            {entriesLoading && entries.length === 0 ? (
              <div className="h-40">
                <CenteredSpinner />
              </div>
            ) : entries.length === 0 ? (
              <div className="card">
                <EmptyState icon={<Brain className="h-5 w-5" />} title={q || category ? 'No matching memories' : 'No memories yet'}>
                  {q || category ? 'Try a different search or category.' : 'Alita saves facts and preferences as you chat, or add one yourself.'}
                </EmptyState>
              </div>
            ) : (
              <>
                <ul className={cx('space-y-2', entriesLoading && 'opacity-60')}>
                  {entries.map((m) => (
                    <MemoryRow key={m.id} entry={m} onChange={changeEntry} onDelete={removeEntry} />
                  ))}
                </ul>
                {hasMore && (
                  <div className="mt-4 flex justify-center">
                    <button className="btn-secondary" onClick={() => loadEntries(true)} disabled={entriesLoading}>
                      {entriesLoading && <Spinner className="h-3.5 w-3.5" />} Load more
                    </button>
                  </div>
                )}
              </>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
