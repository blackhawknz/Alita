'use client';

import { Fragment, useMemo } from 'react';
import { MessageSquareText } from 'lucide-react';
import { DATE_GROUPS, dateGroup, parseUtc, relativeTime } from '@/lib/format';
import type { SearchResult, SessionSummary } from '@/lib/types';
import type { SessionActions } from '../AppShell';
import SessionItem from './SessionItem';

/** Render an FTS snippet, turning **x** marks into highlights. */
function Snippet({ text }: { text: string }) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return (
    <>
      {parts.map((p, i) =>
        p.startsWith('**') && p.endsWith('**') ? (
          <mark key={i} className="rounded bg-primary/20 px-0.5 text-fg">
            {p.slice(2, -2)}
          </mark>
        ) : (
          <Fragment key={i}>{p}</Fragment>
        ),
      )}
    </>
  );
}

export default function SessionList({
  sessions,
  loaded,
  activeId,
  onOpen,
  query,
  messageHits,
  actions,
}: {
  sessions: SessionSummary[];
  loaded: boolean;
  activeId: string | null;
  onOpen: (id: string) => void;
  query: string;
  messageHits: SearchResult[];
  actions: SessionActions;
}) {
  const groups = useMemo(() => {
    const pinned = sessions.filter((s) => s.pinned);
    const rest = sessions.filter((s) => !s.pinned);
    const out: { label: string; items: SessionSummary[] }[] = [];
    if (pinned.length) out.push({ label: 'Pinned', items: pinned });
    const buckets = new Map<string, SessionSummary[]>();
    for (const s of rest) {
      const g = dateGroup(parseUtc(s.updated_at || s.created_at));
      if (!buckets.has(g)) buckets.set(g, []);
      buckets.get(g)!.push(s);
    }
    for (const g of DATE_GROUPS) {
      const items = buckets.get(g);
      if (items?.length) out.push({ label: g, items });
    }
    return out;
  }, [sessions]);

  const searching = query.trim().length > 0;

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
      {loaded && sessions.length === 0 && (
        <p className="px-3 py-6 text-center text-xs text-muted">{searching ? 'No chats match this title.' : 'No chats yet.'}</p>
      )}
      {groups.map((g) => (
        <div key={g.label} className="mt-3 first:mt-1">
          <div className="sticky top-0 z-[1] bg-surface px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-2">
            {g.label}
          </div>
          <ul className="space-y-px">
            {g.items.map((s) => (
              <SessionItem key={s.id} session={s} active={s.id === activeId} onOpen={onOpen} actions={actions} />
            ))}
          </ul>
        </div>
      ))}
      {searching && messageHits.length > 0 && (
        <div className="mt-4">
          <div className="px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-2">In messages</div>
          <ul className="space-y-px">
            {messageHits.map((hit) => (
              <li key={`${hit.session_id}-${hit.message_id}`}>
                <button
                  onClick={() => onOpen(hit.session_id)}
                  className="w-full rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-white/[0.04]"
                >
                  <div className="flex items-center gap-1.5 text-[12px] font-medium text-fg-2">
                    <MessageSquareText className="h-3 w-3 flex-none text-muted" />
                    <span className="truncate">{hit.title || 'Untitled chat'}</span>
                    <span className="ml-auto flex-none text-[10px] text-muted-2">{relativeTime(parseUtc(hit.created_at))}</span>
                  </div>
                  {hit.snippet && (
                    <p className="mt-0.5 line-clamp-2 text-[11.5px] leading-snug text-muted">
                      <Snippet text={hit.snippet} />
                    </p>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
