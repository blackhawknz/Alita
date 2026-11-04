'use client';

import type { RefObject } from 'react';
import {
  Activity,
  Brain,
  Clock,
  Image as ImageIcon,
  Kanban,
  Monitor,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  Settings,
  Sparkles,
  SquarePen,
  UserRound,
  X,
} from 'lucide-react';
import { cx } from '@/lib/format';
import type { Health, SearchResult, SessionSummary } from '@/lib/types';
import type { SessionActions, View } from '../AppShell';
import SessionList from './SessionList';

const NAV: { id: View; label: string; icon: typeof Brain }[] = [
  { id: 'persona', label: 'Persona', icon: UserRound },
  { id: 'memory', label: 'Memory', icon: Brain },
  { id: 'skills', label: 'Skills', icon: Sparkles },
  { id: 'board', label: 'Board', icon: Kanban },
  { id: 'jobs', label: 'Jobs', icon: Clock },
  { id: 'activity', label: 'Activity', icon: Activity },
  { id: 'gallery', label: 'Gallery', icon: ImageIcon },
  { id: 'system', label: 'System', icon: Monitor },
  { id: 'settings', label: 'Settings', icon: Settings },
];

interface Props {
  collapsed: boolean;
  isMobile: boolean;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onToggleCollapsed: () => void;
  view: View;
  onNavigate: (v: View) => void;
  sessions: SessionSummary[];
  sessionsLoaded: boolean;
  activeSessionId: string | null;
  onOpenSession: (id: string) => void;
  onNewChat: () => void;
  query: string;
  onQueryChange: (q: string) => void;
  messageHits: SearchResult[];
  searchRef: RefObject<HTMLInputElement>;
  actions: SessionActions;
  health: Health | null;
  healthError: boolean;
}

const isMac = () => typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);

export default function Sidebar(props: Props) {
  const { collapsed, isMobile, mobileOpen, onCloseMobile } = props;

  if (isMobile) {
    return (
      <>
        {mobileOpen && <div className="fixed inset-0 z-40 bg-black/60 animate-fade-in" onClick={onCloseMobile} aria-hidden />}
        <aside
          className={cx(
            'fixed inset-y-0 left-0 z-50 flex w-[284px] max-w-[85vw] flex-col border-r border-line bg-surface shadow-2xl transition-[transform,visibility] duration-200',
            mobileOpen ? 'translate-x-0' : '-translate-x-full invisible',
          )}
        >
          <ExpandedContent {...props} />
        </aside>
      </>
    );
  }

  return (
    <aside
      className={cx(
        'flex flex-none flex-col border-r border-line bg-surface transition-[width] duration-200',
        collapsed ? 'w-[60px]' : 'w-[272px]',
      )}
    >
      {collapsed ? <Rail {...props} /> : <ExpandedContent {...props} />}
    </aside>
  );
}

function StatusFooter({ health, healthError, compact }: { health: Health | null; healthError: boolean; compact?: boolean }) {
  const ok = !!health && !healthError;
  const label = healthError ? 'Backend offline' : health ? `${health.model}` : 'Connecting…';
  return (
    <div
      className={cx('flex items-center gap-2 text-[11px] text-muted', compact ? 'justify-center py-3' : 'px-4 py-3')}
      title={health ? `${health.llm_provider} · ${health.model} · v${health.version}` : label}
    >
      <span className={cx('h-2 w-2 flex-none rounded-full', ok ? 'bg-teal' : healthError ? 'bg-danger' : 'bg-warn animate-pulse')} />
      {!compact && <span className="truncate font-mono">{label}</span>}
    </div>
  );
}

function Rail(props: Props) {
  const { onToggleCollapsed, onNewChat, view, onNavigate, health, healthError } = props;
  return (
    <div className="flex h-full flex-col items-center gap-1 py-3">
      <button className="icon-btn mb-1" onClick={onToggleCollapsed} title="Expand sidebar" aria-label="Expand sidebar">
        <PanelLeftOpen className="h-[18px] w-[18px]" />
      </button>
      <button className="icon-btn" onClick={onNewChat} title="New chat" aria-label="New chat">
        <SquarePen className="h-[18px] w-[18px]" />
      </button>
      <button
        className="icon-btn"
        onClick={() => {
          onToggleCollapsed();
          setTimeout(() => props.searchRef.current?.focus(), 250);
        }}
        title="Search chats"
        aria-label="Search chats"
      >
        <Search className="h-[18px] w-[18px]" />
      </button>
      <div className="flex-1" />
      {NAV.map((n) => (
        <button
          key={n.id}
          className={cx('icon-btn', view === n.id && 'bg-primary/10 text-primary hover:bg-primary/15 hover:text-primary')}
          onClick={() => onNavigate(n.id)}
          title={n.label}
          aria-label={n.label}
          aria-current={view === n.id ? 'page' : undefined}
        >
          <n.icon className="h-[18px] w-[18px]" />
        </button>
      ))}
      <StatusFooter health={health} healthError={healthError} compact />
    </div>
  );
}

function ExpandedContent(props: Props) {
  const {
    isMobile,
    onCloseMobile,
    onToggleCollapsed,
    view,
    onNavigate,
    onNewChat,
    query,
    onQueryChange,
    searchRef,
    health,
    healthError,
  } = props;
  const mod = isMac() ? 'Cmd' : 'Ctrl';

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-14 flex-none items-center justify-between px-3">
        <button className="flex items-center gap-2 rounded-lg px-1.5 py-1" onClick={onNewChat} title="Alita home">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/logo-trns.png" alt="Alita" className="h-7 w-auto" />
        </button>
        {isMobile ? (
          <button className="icon-btn" onClick={onCloseMobile} title="Close sidebar" aria-label="Close sidebar">
            <X className="h-[18px] w-[18px]" />
          </button>
        ) : (
          <button className="icon-btn" onClick={onToggleCollapsed} title="Collapse sidebar" aria-label="Collapse sidebar">
            <PanelLeftClose className="h-[18px] w-[18px]" />
          </button>
        )}
      </div>

      <div className="flex-none space-y-2 px-3 pb-2">
        <button
          onClick={onNewChat}
          className="group flex w-full items-center gap-2.5 rounded-xl border border-line-strong bg-elevated px-3 py-2 text-[13px] font-medium text-fg transition-colors hover:border-primary/40 hover:bg-overlay"
          title={`New chat (${mod}+Shift+O)`}
        >
          <SquarePen className="h-4 w-4 text-primary" />
          <span className="flex-1 text-left">New chat</span>
          <span className="kbd hidden opacity-0 transition-opacity group-hover:opacity-100 md:inline">{mod}+Shift+O</span>
        </button>
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
          <input
            ref={searchRef}
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                onQueryChange('');
                (e.target as HTMLInputElement).blur();
              }
            }}
            placeholder="Search chats"
            aria-label="Search chats"
            className="w-full rounded-lg border border-transparent bg-white/[0.04] py-1.5 pl-8 pr-14 text-[13px] text-fg outline-none transition-colors placeholder:text-muted-2 focus:border-primary/40 focus:bg-bg"
          />
          {query ? (
            <button
              className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted hover:text-fg"
              onClick={() => onQueryChange('')}
              aria-label="Clear search"
              title="Clear search"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          ) : (
            <span className="kbd pointer-events-none absolute right-2 top-1/2 -translate-y-1/2">{mod}+K</span>
          )}
        </div>
      </div>

      <SessionList
        sessions={props.sessions}
        loaded={props.sessionsLoaded}
        activeId={view === 'chat' ? props.activeSessionId : null}
        onOpen={props.onOpenSession}
        query={query}
        messageHits={props.messageHits}
        actions={props.actions}
      />

      <nav className="flex-none border-t border-line px-2 py-2" aria-label="Main">
        <div className="grid grid-cols-1 gap-0.5">
          {NAV.map((n) => {
            const active = view === n.id;
            return (
              <button
                key={n.id}
                onClick={() => onNavigate(n.id)}
                aria-current={active ? 'page' : undefined}
                className={cx(
                  'flex items-center gap-2.5 rounded-lg px-2.5 py-[7px] text-[13px] font-medium transition-colors',
                  active ? 'bg-primary/10 text-primary' : 'text-muted hover:bg-white/[0.04] hover:text-fg-2',
                )}
              >
                <n.icon className="h-4 w-4 flex-none" />
                {n.label}
              </button>
            );
          })}
        </div>
      </nav>
      <div className="flex-none border-t border-line">
        <StatusFooter health={health} healthError={healthError} />
      </div>
    </div>
  );
}
