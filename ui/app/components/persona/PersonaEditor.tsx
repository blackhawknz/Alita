'use client';

import { useCallback, useEffect, useState } from 'react';
import { Check, Copy, Eye, RotateCcw, Save } from 'lucide-react';
import * as api from '@/lib/api';
import { compactNumber, copyText, cx } from '@/lib/format';
import type { Personality, SystemPromptPreview } from '@/lib/types';
import { useToast } from '../Toaster';
import { CenteredSpinner, Modal, Spinner } from '../ui';

const INSTRUCTION_EXAMPLES =
  'e.g.\n- Answer concisely, use bullet points.\n- I work mostly in TypeScript and Rust.\n- Ask before installing packages.';

/**
 * Editor for Alita's persona (SOUL.md) and the user's custom instructions.
 * Both are injected at the top of every system prompt; changes apply from
 * the next message in every chat.
 */
export default function PersonaEditor({
  compact = false,
  sessionId,
  onSaved,
  onDirtyChange,
}: {
  /** Smaller layout for the quick-edit dialog. */
  compact?: boolean;
  /** Used for the system-prompt preview (includes that chat's summary). */
  sessionId?: string | null;
  onSaved?: () => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const toast = useToast();
  const [data, setData] = useState<Personality | null>(null);
  const [persona, setPersona] = useState('');
  const [instructions, setInstructions] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<SystemPromptPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  const apply = (p: Personality) => {
    setData(p);
    // Show the default text when no custom persona exists, so it can be edited directly.
    setPersona(p.persona || '');
    setInstructions(p.instructions || '');
  };

  const load = useCallback(() => {
    setError(null);
    api
      .getPersonality()
      .then(apply)
      .catch((e) => setError(api.errorMessage(e)));
  }, []);

  useEffect(load, [load]);

  const dirty = !!data && (persona !== (data.persona || '') || instructions !== (data.instructions || ''));
  useEffect(() => onDirtyChange?.(dirty), [dirty, onDirtyChange]);

  const save = async () => {
    setSaving(true);
    try {
      // Saving the unchanged default text would mark the persona "customised"; send empty instead.
      const personaToSave = data && persona.trim() === data.default.trim() ? '' : persona;
      apply(await api.updatePersonality({ persona: personaToSave, instructions }));
      toast.success('Persona saved. It applies from the next message.');
      onSaved?.();
    } catch (e) {
      toast.error(api.errorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    const ok = await toast.confirm({
      title: 'Reset persona?',
      message: 'The persona goes back to the built-in default. Your custom instructions are kept.',
      confirmLabel: 'Reset',
    });
    if (!ok) return;
    try {
      apply(await api.resetPersonality());
      toast.success('Persona reset to default');
      onSaved?.();
    } catch (e) {
      toast.error(api.errorMessage(e));
    }
  };

  const openPreview = async () => {
    if (dirty) toast.info('Preview shows the saved version. Save first to include your edits.');
    setPreviewLoading(true);
    try {
      setPreview(await api.previewSystemPrompt(sessionId));
    } catch (e) {
      toast.error(api.errorMessage(e));
    } finally {
      setPreviewLoading(false);
    }
  };

  const copyPreview = async () => {
    if (preview && (await copyText(preview.prompt))) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };

  if (error) {
    return (
      <div className="space-y-3 text-sm">
        <p className="text-danger">{error}</p>
        <button className="btn-secondary" onClick={load}>
          Retry
        </button>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="h-40">
        <CenteredSpinner />
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div>
        <div className="mb-1.5 flex items-center justify-between gap-2">
          <label className="label mb-0" htmlFor={compact ? 'persona-quick' : 'persona'}>
            Persona / system prompt
          </label>
          <div className="flex items-center gap-2 text-[11px] text-muted">
            <span className={cx('rounded-full px-2 py-0.5', data.is_default ? 'bg-white/[0.06]' : 'bg-primary/10 text-primary')}>
              {data.is_default ? 'Default' : 'Customised'}
            </span>
            <span className="font-mono">{persona.length.toLocaleString()} chars</span>
          </div>
        </div>
        <textarea
          id={compact ? 'persona-quick' : 'persona'}
          value={persona}
          onChange={(e) => setPersona(e.target.value)}
          rows={compact ? 9 : 16}
          placeholder={data.default}
          spellCheck
          className="input resize-y font-mono text-[12.5px] leading-relaxed"
        />
        <div className="mt-1.5 flex flex-wrap items-center justify-between gap-2">
          <p className="hint mt-0">
            Who Alita is: name, personality, tone, expertise and boundaries. Leave empty to use the default.
          </p>
          {data.is_default && !persona && (
            <button type="button" className="text-[11px] font-medium text-primary hover:underline" onClick={() => setPersona(data.default)}>
              Start from the default
            </button>
          )}
        </div>
      </div>

      <div>
        <div className="mb-1.5 flex items-center justify-between">
          <label className="label mb-0" htmlFor={compact ? 'instructions-quick' : 'instructions'}>
            Custom instructions
          </label>
          <span className="font-mono text-[11px] text-muted">{instructions.length.toLocaleString()} chars</span>
        </div>
        <textarea
          id={compact ? 'instructions-quick' : 'instructions'}
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          rows={compact ? 5 : 8}
          placeholder={INSTRUCTION_EXAMPLES}
          className="input resize-y text-[13px] leading-relaxed"
        />
        <p className="hint">Standing preferences added after the persona in every chat.</p>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t border-line pt-4">
        <button className="btn-primary" onClick={save} disabled={!dirty || saving}>
          {saving ? <Spinner className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />} Save
        </button>
        {dirty && (
          <button className="btn-ghost" onClick={() => apply(data)} disabled={saving}>
            Discard changes
          </button>
        )}
        <div className="flex-1" />
        <button className="btn-ghost" onClick={openPreview} disabled={previewLoading} title="See the full system prompt Alita receives">
          {previewLoading ? <Spinner className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />} Preview full prompt
        </button>
        <button className="btn-ghost" onClick={reset} disabled={data.is_default} title="Restore the built-in persona">
          <RotateCcw className="h-3.5 w-3.5" /> Reset
        </button>
      </div>

      {preview && (
        <Modal
          wide
          title="Full system prompt"
          onClose={() => setPreview(null)}
          footer={
            <>
              <span className="mr-auto self-center text-[11px] text-muted">
                {preview.chars.toLocaleString()} chars · ~{compactNumber(preview.approx_tokens)} tokens
              </span>
              <button className="btn-secondary" onClick={copyPreview}>
                {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />} {copied ? 'Copied' : 'Copy'}
              </button>
              <button className="btn-primary" onClick={() => setPreview(null)}>
                Close
              </button>
            </>
          }
        >
          <p className="mb-3 text-xs leading-relaxed text-muted">
            Your persona and instructions come first, followed by Alita&apos;s operating guidelines, tool notes, core memory,
            available skills and environment. Recalled memories and the current time are attached to each message separately.
          </p>
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-xl border border-line bg-bg p-4 font-mono text-[11.5px] leading-relaxed text-fg-2">
            {preview.prompt}
          </pre>
        </Modal>
      )}
    </div>
  );
}
