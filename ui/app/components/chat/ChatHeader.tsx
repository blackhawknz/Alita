'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Archive, Download, Ellipsis, Menu, Mic, MicOff, Pencil, Pin, PinOff, Trash2, UserRound, Volume2, VolumeX } from 'lucide-react';
import type { VoiceConversation } from '@/lib/conversation';
import { compactNumber, cx } from '@/lib/format';
import type { Health, Usage } from '@/lib/types';
import { setAutoSpeak, useAutoSpeak, useVoiceStatus } from '@/lib/voice';
import PersonaEditor from '../persona/PersonaEditor';
import { DropdownMenu, Modal, Spinner, type MenuItemDef } from '../ui';

function ContextMeter({ usage, fallbackWindow }: { usage: Usage | null; fallbackWindow?: number }) {
  const used = usage?.context_tokens || 0;
  const total = usage?.context_window || fallbackWindow || 0;
  if (!total || !used) return null;
  const pct = Math.min(100, (used / total) * 100);
  const color = pct < 50 ? 'bg-teal' : pct < 80 ? 'bg-warn' : 'bg-danger';
  const tip =
    `Context: ${used.toLocaleString()} / ${total.toLocaleString()} tokens (${pct.toFixed(1)}%)` +
    (usage?.cached_tokens ? `\nCached: ${usage.cached_tokens.toLocaleString()}` : '') +
    (usage?.completion_tokens ? `\nLast completion: ${usage.completion_tokens.toLocaleString()}` : '');
  return (
    <div className="hidden items-center gap-2 sm:flex" title={tip} aria-label={tip}>
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-white/[0.08]">
        <div className={cx('h-full rounded-full transition-[width] duration-700', color)} style={{ width: `${Math.max(pct, 2)}%` }} />
      </div>
      <span className="font-mono text-[10.5px] text-muted">
        {compactNumber(used)}/{compactNumber(total)}
      </span>
    </div>
  );
}

function AutoSpeakToggle() {
  const voice = useVoiceStatus();
  const on = useAutoSpeak();
  if (!voice?.configured) return null;
  const label = !voice.voice
    ? "Auto-speak replies (Alita hasn't chosen a voice yet)"
    : on
      ? `Auto-speak is on (${voice.voice.name}). Click to turn off.`
      : 'Auto-speak is off. Click to read new replies aloud.';
  return (
    <button
      className={cx('icon-btn', on && 'text-primary')}
      onClick={() => setAutoSpeak(!on)}
      title={label}
      aria-label={label}
      aria-pressed={on}
    >
      {on ? <Volume2 className="h-[18px] w-[18px]" /> : <VolumeX className="h-[18px] w-[18px]" />}
    </button>
  );
}

export default function ChatHeader({
  title,
  sessionId,
  pinned,
  running,
  health,
  usage,
  isMobile,
  onOpenSidebar,
  onRename,
  onTogglePin,
  onExport,
  onCompact,
  onDelete,
  voice,
}: {
  title: string;
  sessionId: string | null;
  pinned: boolean;
  running: boolean;
  health: Health | null;
  usage: Usage | null;
  isMobile: boolean;
  onOpenSidebar: () => void;
  onRename: (title: string) => void;
  onTogglePin: () => void;
  onExport: () => void;
  onCompact: () => void;
  onDelete: () => void;
  voice: VoiceConversation;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(title);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) {
      setDraft(title);
      setTimeout(() => inputRef.current?.select(), 0);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing]);

  const [personaOpen, setPersonaOpen] = useState(false);
  const personaDirty = useRef(false);
  const onPersonaDirty = useCallback((d: boolean) => {
    personaDirty.current = d;
  }, []);
  const closePersona = useCallback(() => {
    if (personaDirty.current && !window.confirm('Discard unsaved persona changes?')) return;
    personaDirty.current = false;
    setPersonaOpen(false);
  }, []);

  const commit = () => {
    setEditing(false);
    const t = draft.trim();
    if (t && t !== title) onRename(t);
  };

  const items: MenuItemDef[] = [
    { label: 'Rename', icon: <Pencil className="h-3.5 w-3.5" />, onSelect: () => setEditing(true) },
    { label: pinned ? 'Unpin' : 'Pin', icon: pinned ? <PinOff className="h-3.5 w-3.5" /> : <Pin className="h-3.5 w-3.5" />, onSelect: onTogglePin },
    { label: 'Export markdown', icon: <Download className="h-3.5 w-3.5" />, onSelect: onExport },
    { label: 'Compact context', icon: <Archive className="h-3.5 w-3.5" />, onSelect: onCompact, disabled: running },
    { label: 'Delete chat', icon: <Trash2 className="h-3.5 w-3.5" />, onSelect: onDelete, danger: true, disabled: running },
  ];

  return (
    <header className="flex h-14 flex-none items-center gap-2 border-b border-line px-3 md:px-4">
      {isMobile && (
        <button className="icon-btn" onClick={onOpenSidebar} title="Open sidebar" aria-label="Open sidebar">
          <Menu className="h-5 w-5" />
        </button>
      )}
      <div className="flex min-w-0 flex-1 items-center gap-2">
        {editing ? (
          <input
            ref={inputRef}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commit}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commit();
              if (e.key === 'Escape') setEditing(false);
            }}
            maxLength={120}
            aria-label="Chat title"
            className="w-full max-w-md rounded-lg border border-primary/50 bg-bg px-2 py-1 text-sm font-medium text-fg outline-none ring-2 ring-primary/15"
          />
        ) : (
          <button
            className={cx('min-w-0 truncate rounded-lg px-2 py-1 text-left text-sm font-medium text-fg', sessionId && 'hover:bg-white/5')}
            onClick={() => sessionId && setEditing(true)}
            disabled={!sessionId}
            title={sessionId ? 'Click to rename' : undefined}
          >
            {title}
          </button>
        )}
        {running && <Spinner className="h-3.5 w-3.5 flex-none text-primary" />}
      </div>

      <div className="flex flex-none items-center gap-3">
        <ContextMeter usage={usage} fallbackWindow={health?.context_window} />
        {health && (
          <span
            className="hidden max-w-[220px] truncate rounded-full border border-line-strong bg-elevated px-2.5 py-0.5 font-mono text-[11px] text-fg-2 md:inline-block"
            title={`${health.llm_provider} · ${health.model}`}
          >
            {health.model}
          </span>
        )}
        <button
          className={cx('icon-btn', voice.enabled && 'text-primary')}
          onClick={voice.toggle}
          title={
            voice.supported
              ? voice.enabled
                ? `Hands-free voice conversation is on (${voice.phase}). Click to end.`
                : 'Start hands-free voice conversation'
              : 'Voice conversation needs Chrome or Edge'
          }
          aria-label="Toggle hands-free voice conversation"
          aria-pressed={voice.enabled}
        >
          {voice.enabled ? (
            <Mic className={cx('h-[18px] w-[18px]', (voice.phase === 'listening' || voice.phase === 'speaking') && 'animate-pulse')} />
          ) : (
            <MicOff className="h-[18px] w-[18px]" />
          )}
        </button>
        <AutoSpeakToggle />
        <button
          className="icon-btn"
          onClick={() => setPersonaOpen(true)}
          title="Edit persona and custom instructions"
          aria-label="Edit persona and custom instructions"
        >
          <UserRound className="h-[18px] w-[18px]" />
        </button>
        {sessionId && <DropdownMenu label="Chat options" trigger={<Ellipsis className="h-[18px] w-[18px]" />} items={items} />}
      </div>
      {personaOpen && (
        <Modal wide title="Persona and custom instructions" onClose={closePersona}>
          <p className="mb-4 text-xs leading-relaxed text-muted">
            Alita&apos;s system prompt. Changes apply from your next message, in this chat and every other.
          </p>
          <PersonaEditor
            compact
            sessionId={sessionId}
            onDirtyChange={onPersonaDirty}
            onSaved={() => {
              personaDirty.current = false;
            }}
          />
        </Modal>
      )}
    </header>
  );
}
