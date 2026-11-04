'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import * as api from '@/lib/api';
import { downloadText, safeFilename } from '@/lib/format';
import type { Health, SearchResult, SessionSummary, SlashCommand } from '@/lib/types';
import { ToastProvider, useToast } from './Toaster';
import Sidebar from './sidebar/Sidebar';
import ChatView from './chat/ChatView';
import MemoryView from './MemoryView';
import PersonaView from './persona/PersonaView';
import SkillsView from './SkillsView';
import SettingsView from './settings/SettingsView';
import SystemView from './SystemView';
import KanbanView from './KanbanView';
import JobsView from './JobsView';
import GalleryView from './GalleryView';
import ActivityView from './ActivityView';
import MobileTopBar from './MobileTopBar';

export type View =
  | 'chat'
  | 'persona'
  | 'memory'
  | 'skills'
  | 'board'
  | 'jobs'
  | 'activity'
  | 'gallery'
  | 'system'
  | 'settings';
const VIEWS: View[] = [
  'chat',
  'persona',
  'memory',
  'skills',
  'board',
  'jobs',
  'activity',
  'gallery',
  'system',
  'settings',
];

export interface SessionActions {
  rename: (id: string, title: string) => Promise<boolean>;
  togglePin: (id: string, pinned: boolean) => Promise<void>;
  exportMd: (id: string, title?: string) => Promise<void>;
  remove: (id: string) => Promise<boolean>;
}

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const update = () => setMatches(mq.matches);
    update();
    mq.addEventListener('change', update);
    return () => mq.removeEventListener('change', update);
  }, [query]);
  return matches;
}

export default function AppShell() {
  return (
    <ToastProvider>
      <Shell />
    </ToastProvider>
  );
}

function Shell() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const toast = useToast();

  const rawView = params.get('view') as View | null;
  const view: View = rawView && VIEWS.includes(rawView) ? rawView : 'chat';
  const sessionId = view === 'chat' ? params.get('s') : null;

  const isMobile = useMediaQuery('(max-width: 767px)');
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);

  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [query, setQuery] = useState('');
  const [messageHits, setMessageHits] = useState<SearchResult[]>([]);
  const [commands, setCommands] = useState<SlashCommand[]>([]);
  const [newChatNonce, setNewChatNonce] = useState(0);
  const searchRef = useRef<HTMLInputElement>(null);
  const queryRef = useRef(query);
  queryRef.current = query;

  // ─── Navigation ───────────────────────────────────────────────
  const navigate = useCallback(
    (next: { view?: View; s?: string | null }, opts: { replace?: boolean } = {}) => {
      const v = next.view ?? 'chat';
      const sp = new URLSearchParams();
      sp.set('view', v);
      if (v === 'chat' && next.s) sp.set('s', next.s);
      const url = `${pathname}?${sp.toString()}`;
      if (opts.replace) router.replace(url, { scroll: false });
      else router.push(url, { scroll: false });
      if (isMobile) setMobileOpen(false);
    },
    [pathname, router, isMobile],
  );

  const openSession = useCallback((id: string) => navigate({ view: 'chat', s: id }), [navigate]);
  const newChat = useCallback(() => {
    navigate({ view: 'chat', s: null });
    setNewChatNonce((n) => n + 1);
  }, [navigate]);

  // ─── Persisted sidebar state ──────────────────────────────────
  useEffect(() => {
    setCollapsed(localStorage.getItem('alita:sidebar-collapsed') === '1');
  }, []);
  const toggleCollapsed = () => {
    setCollapsed((c) => {
      localStorage.setItem('alita:sidebar-collapsed', c ? '0' : '1');
      return !c;
    });
  };

  // ─── Health ───────────────────────────────────────────────────
  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.getHealth());
      setHealthError(false);
    } catch {
      setHealthError(true);
    }
  }, []);

  useEffect(() => {
    refreshHealth();
    const t = setInterval(refreshHealth, 30000);
    return () => clearInterval(t);
  }, [refreshHealth]);

  useEffect(() => {
    api.listCommands().then(setCommands).catch(() => {});
  }, []);

  // ─── Sessions ─────────────────────────────────────────────────
  const refreshSessions = useCallback(async () => {
    try {
      const list = await api.listSessions(queryRef.current.trim());
      setSessions(list);
    } catch {
      /* backend offline: keep the last list */
    } finally {
      setSessionsLoaded(true);
    }
  }, []);

  useEffect(() => {
    const q = query.trim();
    const t = setTimeout(() => {
      refreshSessions();
      if (q.length >= 2) {
        api.searchMessages(q, 12).then(setMessageHits).catch(() => setMessageHits([]));
      } else {
        setMessageHits([]);
      }
    }, q ? 250 : 0);
    return () => clearTimeout(t);
  }, [query, refreshSessions]);

  const anyRunning = sessions.some((s) => s.is_running);
  useEffect(() => {
    const t = setInterval(refreshSessions, anyRunning ? 4000 : 30000);
    return () => clearInterval(t);
  }, [anyRunning, refreshSessions]);

  const patchSessionLocal = useCallback((id: string, patch: Partial<SessionSummary>) => {
    setSessions((list) => list.map((s) => (s.id === id ? { ...s, ...patch } : s)));
  }, []);

  const actions: SessionActions = useMemo(
    () => ({
      rename: async (id, title) => {
        const t = title.trim();
        if (!t) return false;
        patchSessionLocal(id, { title: t });
        try {
          await api.updateSession(id, { title: t });
          return true;
        } catch (e) {
          toast.error(`Rename failed: ${api.errorMessage(e)}`);
          refreshSessions();
          return false;
        }
      },
      togglePin: async (id, pinned) => {
        patchSessionLocal(id, { pinned });
        try {
          await api.updateSession(id, { pinned });
          refreshSessions();
        } catch (e) {
          toast.error(`Could not ${pinned ? 'pin' : 'unpin'}: ${api.errorMessage(e)}`);
          refreshSessions();
        }
      },
      exportMd: async (id, title) => {
        try {
          const md = await api.exportSession(id);
          downloadText(`${safeFilename(title || 'chat')}.md`, md);
        } catch (e) {
          toast.error(`Export failed: ${api.errorMessage(e)}`);
        }
      },
      remove: async (id) => {
        const s = sessions.find((x) => x.id === id);
        const ok = await toast.confirm({
          title: 'Delete chat?',
          message: (
            <>
              <span className="font-medium text-fg">{s?.title || 'This chat'}</span> and all its messages will be permanently
              deleted.
            </>
          ),
          confirmLabel: 'Delete',
          danger: true,
        });
        if (!ok) return false;
        try {
          await api.deleteSession(id);
          setSessions((list) => list.filter((x) => x.id !== id));
          toast.success('Chat deleted');
          if (id === sessionId) navigate({ view: 'chat', s: null }, { replace: true });
          return true;
        } catch (e) {
          toast.error(
            e instanceof api.ApiError && e.status === 409
              ? 'This chat is still running. Stop it first.'
              : `Delete failed: ${api.errorMessage(e)}`,
          );
          return false;
        }
      },
    }),
    [patchSessionLocal, refreshSessions, sessions, sessionId, navigate, toast],
  );

  // ─── Keyboard shortcuts ───────────────────────────────────────
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && !e.shiftKey && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        if (isMobile) setMobileOpen(true);
        else if (collapsed) setCollapsed(false);
        setTimeout(() => searchRef.current?.focus(), 30);
      } else if (mod && e.shiftKey && e.key.toLowerCase() === 'o') {
        e.preventDefault();
        newChat();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isMobile, collapsed, newChat]);

  const activeSession = sessions.find((s) => s.id === sessionId) || null;

  const onSessionCreated = useCallback(
    (id: string, title?: string) => {
      setSessions((list) =>
        list.some((s) => s.id === id)
          ? list
          : [
              {
                id,
                title: title || 'New chat',
                kind: 'chat',
                pinned: false,
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString(),
                message_count: 1,
                is_running: true,
                compacted: false,
                parent_id: null,
              },
              ...list,
            ],
      );
      navigate({ view: 'chat', s: id }, { replace: true });
    },
    [navigate],
  );

  const sidebar = (
    <Sidebar
      collapsed={!isMobile && collapsed}
      isMobile={isMobile}
      mobileOpen={mobileOpen}
      onCloseMobile={() => setMobileOpen(false)}
      onToggleCollapsed={toggleCollapsed}
      view={view}
      onNavigate={(v) => navigate({ view: v, s: v === 'chat' ? sessionId : null })}
      sessions={sessions}
      sessionsLoaded={sessionsLoaded}
      activeSessionId={sessionId}
      onOpenSession={openSession}
      onNewChat={newChat}
      query={query}
      onQueryChange={setQuery}
      messageHits={messageHits}
      searchRef={searchRef}
      actions={actions}
      health={health}
      healthError={healthError}
    />
  );

  const openSidebar = () => (isMobile ? setMobileOpen(true) : setCollapsed(false));

  return (
    <div className="flex h-screen w-full overflow-hidden bg-bg">
      {sidebar}
      <main className="flex min-w-0 flex-1 flex-col">
        {view !== 'chat' && isMobile && <MobileTopBar onMenu={openSidebar} />}
        <div className="min-h-0 flex-1">
          {view === 'chat' && (
            <ChatView
              sessionId={sessionId}
              session={activeSession}
              newChatNonce={newChatNonce}
              health={health}
              commands={commands}
              isMobile={isMobile}
              onOpenSidebar={openSidebar}
              onSessionCreated={onSessionCreated}
              onSessionsChanged={refreshSessions}
              onTitle={(id, title) => patchSessionLocal(id, { title })}
              onRunningChange={(id, running) => patchSessionLocal(id, { is_running: running })}
              onNavigateSession={(id) => (id ? openSession(id) : newChat())}
              actions={actions}
            />
          )}
          {view === 'persona' && <PersonaView />}
          {view === 'memory' && <MemoryView />}
          {view === 'skills' && <SkillsView />}
          {view === 'board' && <KanbanView />}
          {view === 'jobs' && <JobsView />}
          {view === 'activity' && <ActivityView />}
          {view === 'gallery' && <GalleryView />}
          {view === 'system' && <SystemView health={health} onRefreshHealth={refreshHealth} onOpenSession={openSession} />}
          {view === 'settings' && <SettingsView onSaved={refreshHealth} />}
        </div>
      </main>
    </div>
  );
}
