'use client';

import { useEffect, useRef, useState } from 'react';
import { Download, Ellipsis, Pencil, Pin, PinOff, Trash2 } from 'lucide-react';
import { cx } from '@/lib/format';
import type { SessionSummary } from '@/lib/types';
import type { SessionActions } from '../AppShell';
import { DropdownMenu, Spinner } from '../ui';

export default function SessionItem({
  session,
  active,
  onOpen,
  actions,
}: {
  session: SessionSummary;
  active: boolean;
  onOpen: (id: string) => void;
  actions: SessionActions;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(session.title);
  const [menuOpen, setMenuOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) {
      setDraft(session.title);
      setTimeout(() => inputRef.current?.select(), 0);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing]);

  const commit = async () => {
    const t = draft.trim();
    setEditing(false);
    if (t && t !== session.title) await actions.rename(session.id, t);
  };

  if (editing) {
    return (
      <li className="px-1 py-0.5">
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
          className="w-full rounded-lg border border-primary/50 bg-bg px-2 py-1.5 text-[13px] text-fg outline-none ring-2 ring-primary/15"
        />
      </li>
    );
  }

  return (
    <li className="group relative">
      <button
        onClick={() => onOpen(session.id)}
        onDoubleClick={() => setEditing(true)}
        className={cx(
          'flex w-full items-center gap-2 rounded-lg py-[7px] pl-2.5 pr-9 text-left text-[13px] transition-colors',
          active ? 'bg-white/[0.07] text-fg' : 'text-fg-2 hover:bg-white/[0.04]',
        )}
        aria-current={active ? 'true' : undefined}
        title={session.title}
      >
        {session.is_running && <Spinner className="h-3.5 w-3.5 flex-none text-primary" />}
        <span className="truncate">{session.title || 'New chat'}</span>
      </button>
      <div
        className={cx(
          'absolute inset-y-0 right-1 flex items-center transition-opacity',
          menuOpen || active ? 'opacity-100' : 'opacity-0 focus-within:opacity-100 group-hover:opacity-100',
        )}
      >
        <DropdownMenu
          label="Chat options"
          trigger={<Ellipsis className="h-4 w-4" />}
          onOpenChange={setMenuOpen}
          items={[
            { label: 'Rename', icon: <Pencil className="h-3.5 w-3.5" />, onSelect: () => setEditing(true) },
            {
              label: session.pinned ? 'Unpin' : 'Pin',
              icon: session.pinned ? <PinOff className="h-3.5 w-3.5" /> : <Pin className="h-3.5 w-3.5" />,
              onSelect: () => actions.togglePin(session.id, !session.pinned),
            },
            { label: 'Export markdown', icon: <Download className="h-3.5 w-3.5" />, onSelect: () => actions.exportMd(session.id, session.title) },
            { label: 'Delete', icon: <Trash2 className="h-3.5 w-3.5" />, danger: true, onSelect: () => actions.remove(session.id) },
          ]}
        />
      </div>
    </li>
  );
}
