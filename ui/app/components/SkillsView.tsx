'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { ArrowLeft, BookOpen, FileCode, Pencil, Plus, Search, Sparkles, Trash2, X } from 'lucide-react';
import * as api from '@/lib/api';
import { cx, parseLocal, parseUtc, relativeTime } from '@/lib/format';
import type { Skill, SkillDetail } from '@/lib/types';
import Markdown from './chat/Markdown';
import { useToast } from './Toaster';
import { CenteredSpinner, EmptyState, PageHeader, Spinner } from './ui';

// Mirrors backend validation (skills/manager.py): 2-64 chars, description >= 10, content >= 20.
const NAME_RE = /^[a-z0-9][a-z0-9-]{1,63}$/;
const MIN_DESC = 10;
const MIN_CONTENT = 20;

function SourceBadge({ source }: { source: string }) {
  return (
    <span
      className={cx(
        'rounded px-1.5 py-px text-[10px] font-semibold uppercase tracking-wide',
        source === 'user' ? 'bg-teal/10 text-teal' : 'bg-primary/10 text-primary',
      )}
    >
      {source === 'user' ? 'User' : 'Built-in'}
    </span>
  );
}

interface FormState {
  mode: 'create' | 'edit';
  name: string;
  description: string;
  tags: string;
  content: string;
  source?: string;
}

function SkillForm({
  initial,
  onCancel,
  onSaved,
}: {
  initial: FormState;
  onCancel: () => void;
  onSaved: (name: string) => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState(initial);
  const [tab, setTab] = useState<'write' | 'preview'>('write');
  const [saving, setSaving] = useState(false);
  const nameValid = NAME_RE.test(form.name);
  const descValid = form.description.trim().length >= MIN_DESC;
  const contentValid = form.content.trim().length >= MIN_CONTENT;
  const valid = nameValid && descValid && contentValid;

  const set = (patch: Partial<FormState>) => setForm((f) => ({ ...f, ...patch }));

  const save = async () => {
    if (!valid) return;
    setSaving(true);
    const tags = form.tags
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean);
    try {
      if (form.mode === 'create') {
        await api.createSkill({ name: form.name, description: form.description.trim(), content: form.content, tags });
        toast.success(`Skill “${form.name}” created`);
      } else {
        await api.updateSkill(form.name, { description: form.description.trim(), content: form.content, tags });
        toast.success(form.source === 'builtin' ? `Saved a user override of “${form.name}”` : `Skill “${form.name}” saved`);
      }
      onSaved(form.name);
    } catch (e) {
      toast.error(api.errorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-none items-center justify-between gap-3 border-b border-line px-5 py-3">
        <h2 className="text-sm font-semibold text-fg">{form.mode === 'create' ? 'New skill' : `Edit ${form.name}`}</h2>
        <div className="flex gap-2">
          <button className="btn-ghost" onClick={onCancel}>
            Cancel
          </button>
          <button className="btn-primary" onClick={save} disabled={!valid || saving}>
            {saving && <Spinner className="h-3.5 w-3.5" />} {form.mode === 'create' ? 'Create skill' : 'Save'}
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
        {form.mode === 'edit' && form.source === 'builtin' && (
          <p className="rounded-lg border border-primary/25 bg-primary/[0.06] px-3 py-2 text-xs text-fg-2">
            This is a built-in skill. Saving creates a user copy that overrides it; delete the copy to restore the original.
          </p>
        )}
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <div>
            <label className="label" htmlFor="skill-name">
              Name
            </label>
            <input
              id="skill-name"
              value={form.name}
              disabled={form.mode === 'edit'}
              onChange={(e) => set({ name: e.target.value.toLowerCase().replace(/[\s_]+/g, '-').replace(/[^a-z0-9-]/g, '') })}
              placeholder="e.g. deploy-docker-app"
              className={cx('input font-mono', form.name && !nameValid && 'border-danger/60')}
            />
            <p className={cx('hint', form.name && !nameValid && 'text-danger')}>
              {form.name && !nameValid ? '2-64 characters: lowercase letters, digits and hyphens.' : 'kebab-case, unique.'}
            </p>
          </div>
          <div>
            <label className="label" htmlFor="skill-tags">
              Tags
            </label>
            <input id="skill-tags" value={form.tags} onChange={(e) => set({ tags: e.target.value })} placeholder="docker, deploy" className="input" />
            <p className="hint">Comma-separated.</p>
          </div>
        </div>
        <div>
          <label className="label" htmlFor="skill-desc">
            Description
          </label>
          <input
            id="skill-desc"
            value={form.description}
            onChange={(e) => set({ description: e.target.value })}
            placeholder="When to use this skill, in one sentence"
            className="input"
          />
          <p className={cx('hint', form.description && !descValid && 'text-warn')}>
            {form.description && !descValid
              ? `At least ${MIN_DESC} characters: say what it does and when to use it.`
              : 'Alita reads this to decide when the skill is relevant.'}
          </p>
        </div>
        <div>
          <div className="mb-1.5 flex items-center justify-between">
            <span className="label mb-0">Instructions (markdown)</span>
            <div className="flex rounded-lg border border-line-strong p-0.5 text-xs">
              {(['write', 'preview'] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={cx('rounded-md px-2.5 py-0.5 capitalize', tab === t ? 'bg-white/10 text-fg' : 'text-muted hover:text-fg-2')}
                >
                  {t}
                </button>
              ))}
            </div>
          </div>
          {tab === 'write' ? (
            <textarea
              value={form.content}
              onChange={(e) => set({ content: e.target.value })}
              rows={20}
              placeholder={'# Steps\n1. ...\n\n## Pitfalls\n- ...'}
              className="input min-h-[360px] resize-y font-mono text-[12.5px] leading-relaxed"
              aria-label="Skill instructions"
            />
          ) : (
            <div className="min-h-[360px] rounded-lg border border-line-strong bg-bg px-4 py-3">
              {form.content.trim() ? <Markdown text={form.content} /> : <p className="text-sm text-muted">Nothing to preview.</p>}
            </div>
          )}
          {form.content && !contentValid && <p className="hint text-warn">At least {MIN_CONTENT} characters of instructions are required.</p>}
        </div>
      </div>
    </div>
  );
}

function SkillDetailPane({
  name,
  onEdit,
  onDeleted,
  onBack,
}: {
  name: string;
  onEdit: (d: SkillDetail) => void;
  onDeleted: () => void;
  onBack?: () => void;
}) {
  const toast = useToast();
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    api
      .getSkill(name)
      .then((d) => !cancelled && setDetail(d))
      .catch((e) => !cancelled && setError(api.errorMessage(e)));
    return () => {
      cancelled = true;
    };
  }, [name]);

  const remove = async () => {
    if (!detail) return;
    const ok = await toast.confirm({
      title: `Delete “${detail.name}”?`,
      message: 'This removes the skill folder from your skills directory.',
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteSkill(detail.name);
      toast.success('Skill deleted');
      onDeleted();
    } catch (e) {
      toast.error(api.errorMessage(e));
    }
  };

  if (error) return <EmptyState icon={<BookOpen className="h-5 w-5" />} title="Could not load skill">{error}</EmptyState>;
  if (!detail) return <CenteredSpinner />;

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-none items-start justify-between gap-3 border-b border-line px-5 py-3.5">
        <div className="flex min-w-0 items-start gap-2">
          {onBack && (
            <button className="icon-btn -ml-2" onClick={onBack} aria-label="Back to list" title="Back">
              <ArrowLeft className="h-4 w-4" />
            </button>
          )}
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="truncate font-mono text-[15px] font-semibold text-fg">{detail.name}</h2>
              <SourceBadge source={detail.source} />
            </div>
            <p className="mt-0.5 text-[13px] text-muted">{detail.description}</p>
          </div>
        </div>
        <div className="flex flex-none gap-2">
          <button className="btn-secondary" onClick={() => onEdit(detail)}>
            <Pencil className="h-3.5 w-3.5" /> {detail.editable ? 'Edit' : 'Customize'}
          </button>
          {detail.editable && (
            <button className="btn-danger" onClick={remove}>
              <Trash2 className="h-3.5 w-3.5" /> Delete
            </button>
          )}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11.5px] text-muted">
          <span>Used {detail.usage_count}x</span>
          {detail.last_used && <span>Last used {relativeTime(parseUtc(detail.last_used))}</span>}
          {detail.updated_at && <span>Updated {relativeTime(parseLocal(detail.updated_at))}</span>}
          {detail.tags.length > 0 && (
            <span className="flex flex-wrap gap-1">
              {detail.tags.map((t) => (
                <span key={t} className="chip py-0 text-[10.5px]">
                  {t}
                </span>
              ))}
            </span>
          )}
        </div>
        {detail.files.length > 0 && (
          <div className="mb-4 rounded-lg border border-line bg-surface px-3 py-2">
            <p className="mb-1 text-[10.5px] font-semibold uppercase tracking-wider text-muted-2">Supporting files</p>
            <ul className="space-y-0.5">
              {detail.files.map((f) => (
                <li key={f} className="flex items-center gap-1.5 font-mono text-xs text-fg-2">
                  <FileCode className="h-3 w-3 text-muted" />
                  {f}
                </li>
              ))}
            </ul>
          </div>
        )}
        <Markdown text={detail.content || '_No instructions._'} />
        <p className="mt-6 truncate font-mono text-[10.5px] text-muted-2" title={detail.path}>
          {detail.path}
        </p>
      </div>
    </div>
  );
}

export default function SkillsView() {
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [source, setSource] = useState<'all' | 'user' | 'builtin'>('all');
  const [selected, setSelected] = useState<string | null>(null);
  const [form, setForm] = useState<FormState | null>(null);

  const load = useCallback(async () => {
    try {
      setSkills(await api.listSkills());
      setError(null);
    } catch (e) {
      setError(api.errorMessage(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (skills || []).filter(
      (s) =>
        (source === 'all' || s.source === source) &&
        (!q || s.name.includes(q) || s.description.toLowerCase().includes(q) || s.tags.some((t) => t.toLowerCase().includes(q))),
    );
  }, [skills, search, source]);

  const showDetailOnMobile = !!(selected || form);

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Skills"
        icon={<Sparkles className="h-4 w-4" />}
        subtitle={skills ? `${skills.length} skills · reusable procedures Alita can follow` : 'Loading…'}
        actions={
          <button
            className="btn-primary"
            onClick={() => {
              setSelected(null);
              setForm({ mode: 'create', name: '', description: '', tags: '', content: '' });
            }}
          >
            <Plus className="h-4 w-4" /> <span className="hidden sm:inline">New skill</span>
          </button>
        }
      />
      <div className="flex min-h-0 flex-1">
        <div className={cx('flex w-full flex-col border-r border-line md:w-[340px] md:flex-none', showDetailOnMobile && 'hidden md:flex')}>
          <div className="flex-none space-y-2 border-b border-line p-3">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search skills" className="input pl-9" aria-label="Search skills" />
              {search && (
                <button className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-fg" onClick={() => setSearch('')} aria-label="Clear search" title="Clear">
                  <X className="h-4 w-4" />
                </button>
              )}
            </div>
            <div className="flex gap-1.5">
              {(['all', 'user', 'builtin'] as const).map((s) => (
                <button
                  key={s}
                  onClick={() => setSource(s)}
                  className={cx(
                    'rounded-full border px-2.5 py-0.5 text-[11.5px] font-medium transition-colors',
                    source === s ? 'border-primary/40 bg-primary/10 text-primary' : 'border-line-strong text-muted hover:text-fg-2',
                  )}
                >
                  {s === 'all' ? 'All' : s === 'user' ? 'User' : 'Built-in'}
                </button>
              ))}
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            {error && <p className="p-4 text-center text-xs text-danger">{error}</p>}
            {!skills && !error && <CenteredSpinner />}
            {skills && filtered.length === 0 && (
              <p className="p-6 text-center text-xs text-muted">
                {skills.length === 0 ? 'No skills yet. Alita creates them after complex tasks.' : 'No matching skills.'}
              </p>
            )}
            <ul className="space-y-1">
              {filtered.map((s) => (
                <li key={s.name}>
                  <button
                    onClick={() => {
                      setForm(null);
                      setSelected(s.name);
                    }}
                    className={cx(
                      'w-full rounded-lg px-3 py-2.5 text-left transition-colors',
                      selected === s.name && !form ? 'bg-white/[0.07]' : 'hover:bg-white/[0.04]',
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <span className="truncate font-mono text-[13px] font-medium text-fg">{s.name}</span>
                      <SourceBadge source={s.source} />
                      {s.usage_count > 0 && <span className="ml-auto flex-none text-[10.5px] text-muted-2">{s.usage_count}x</span>}
                    </div>
                    <p className="mt-0.5 line-clamp-2 text-xs leading-snug text-muted">{s.description}</p>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </div>
        <div className={cx('min-w-0 flex-1', !showDetailOnMobile && 'hidden md:block')}>
          {form ? (
            <SkillForm
              key={`${form.mode}-${form.name}`}
              initial={form}
              onCancel={() => setForm(null)}
              onSaved={(name) => {
                setForm(null);
                setSelected(name);
                load();
              }}
            />
          ) : selected ? (
            <SkillDetailPane
              key={selected}
              name={selected}
              onBack={() => setSelected(null)}
              onEdit={(d) =>
                setForm({ mode: 'edit', name: d.name, description: d.description, tags: d.tags.join(', '), content: d.content, source: d.source })
              }
              onDeleted={() => {
                setSelected(null);
                load();
              }}
            />
          ) : (
            <EmptyState icon={<BookOpen className="h-5 w-5" />} title="Select a skill">
              Skills are markdown playbooks. Alita picks relevant ones automatically, and creates new ones after complex tasks.
            </EmptyState>
          )}
        </div>
      </div>
    </div>
  );
}
