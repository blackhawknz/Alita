'use client';

// Chat state machine for one session: history loading, live turn streaming
// (with re-attach), sending / editing / regenerating, approvals and stop.

import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '@/lib/api';
import { attachSessionStream, startChatStream, type StreamOutcome } from '@/lib/stream';
import {
  finishTurn,
  groupHistory,
  lastUserIndex,
  newTurn,
  reduceTurn,
  todosFromResult,
  turnText,
  uid,
  type AssistantTurn,
  type ChatItem,
  type UserItem,
} from '@/lib/turns';
import { maybeAutoSpeak, refreshVoiceStatus } from '@/lib/voice';
import type { ChatRequest, SlashCommand, StreamEvent, Todo, UploadResult, Usage } from '@/lib/types';

const TURN_EVENTS = new Set<StreamEvent['type']>([
  'turn_start', 'memory_recall', 'skills_suggested', 'step_start', 'snapshot', 'text', 'reasoning', 'message',
  'tool_call', 'approval_requested', 'approval', 'tool_result', 'compaction', 'error', 'done',
]);

export interface ChatCallbacks {
  onSessionCreated: (id: string, title?: string) => void;
  onSessionsChanged: () => void;
  onTitle: (id: string, title: string) => void;
  onRunningChange: (id: string, running: boolean) => void;
  onNavigateSession: (id: string | null) => void;
  toastError: (msg: string) => void;
  toastInfo: (msg: string) => void;
}

interface SendOptions {
  text: string;
  uploads?: UploadResult[];
  editMessageId?: number;
  regenerate?: boolean;
}

interface PendingRequest {
  text: string;
  uploads: UploadResult[];
  userKey: string | null;
}

const uploadIdFromUrl = (url: string) => /\/uploads\/([0-9a-f]{16})/.exec(url)?.[1] || null;

export function useChat(sessionId: string | null, newChatNonce: number, commands: SlashCommand[], cb: ChatCallbacks) {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [live, setLive] = useState<AssistantTurn | null>(null);
  const [running, setRunning] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [todos, setTodos] = useState<Todo[]>([]);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [title, setTitle] = useState<string | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [scrollNonce, setScrollNonce] = useState(0);

  const sidRef = useRef<string | null>(null);
  const genRef = useRef(0);
  const liveRef = useRef<AssistantTurn | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const rafRef = useRef<number | null>(null);
  const runningRef = useRef(false);
  const idleRef = useRef(false);
  const pendingNavRef = useRef<string | null>(null);
  const requestsRef = useRef(new Map<string, PendingRequest>());
  const currentReqRef = useRef<PendingRequest | null>(null);
  const nonceRef = useRef(newChatNonce);
  const cbRef = useRef(cb);
  cbRef.current = cb;
  const commandsRef = useRef(commands);
  commandsRef.current = commands;

  const scheduleFlush = useCallback(() => {
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      setLive(liveRef.current);
    });
  }, []);

  const cancelFlush = () => {
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
  };

  const setRunningBoth = (v: boolean) => {
    runningRef.current = v;
    setRunning(v);
    if (!v) setStopping(false);
    const sid = sidRef.current;
    if (sid) cbRef.current.onRunningChange(sid, v);
  };

  // ─── Event handling ───────────────────────────────────────────
  const handleEvent = useCallback(
    (ev: StreamEvent, gen: number) => {
      if (gen !== genRef.current) return;
      switch (ev.type) {
        case 'session':
          if (!sidRef.current && ev.session_id) {
            sidRef.current = ev.session_id;
            cbRef.current.onSessionCreated(ev.session_id, ev.title);
            cbRef.current.onRunningChange(ev.session_id, true);
          }
          if (ev.title) setTitle(ev.title);
          break;
        case 'turn_start': {
          const userKey = currentReqRef.current?.userKey;
          if (userKey && typeof ev.user_message_id === 'number') {
            const id = ev.user_message_id;
            setItems((prev) => prev.map((it) => (it.kind === 'user' && it.key === userKey ? { ...it, dbId: id } : it)));
          }
          if (ev.title) setTitle(ev.title);
          else setTimeout(() => cbRef.current.onSessionsChanged(), 600); // pick up the fallback title
          break;
        }
        case 'title':
          setTitle(ev.title);
          if (sidRef.current) cbRef.current.onTitle(sidRef.current, ev.title);
          break;
        case 'usage':
          setUsage({
            context_tokens: ev.context_tokens,
            completion_tokens: ev.completion_tokens,
            cached_tokens: ev.cached_tokens,
            context_window: ev.context_window,
          });
          break;
        case 'tool_result':
          if (ev.tool === 'todo') {
            const t = todosFromResult(ev.result);
            if (t) setTodos(t);
          }
          if (ev.tool === 'voice' && ev.success) void refreshVoiceStatus();
          break;
        case 'command': {
          const req = currentReqRef.current;
          const userKey = req?.userKey;
          setItems((prev) => [
            ...prev.filter((it) => it.key !== userKey),
            { kind: 'command', key: uid('cmd'), input: req?.text || ev.command, message: ev.message, level: ev.level },
          ]);
          liveRef.current = null;
          setLive(null);
          if (ev.action === 'switch_session' && ev.session_id && ev.session_id !== sidRef.current) {
            pendingNavRef.current = ev.session_id;
          }
          if (ev.action === 'refresh_sessions' || ev.action === 'switch_session') cbRef.current.onSessionsChanged();
          break;
        }
        case 'idle':
          idleRef.current = true;
          break;
      }
      if (TURN_EVENTS.has(ev.type) && liveRef.current) {
        liveRef.current = reduceTurn(liveRef.current, ev);
        scheduleFlush();
      }
    },
    [scheduleFlush],
  );

  // ─── Loading a session ────────────────────────────────────────
  // `attach` and `load` call each other; route through a ref to keep callbacks stable.
  const attachRef = useRef<(id: string, gen: number) => Promise<void>>(async () => {});

  const load = useCallback(async (id: string, gen: number) => {
    setLoading(true);
    setLoadError(null);
    try {
      const data = await api.getMessages(id);
      if (gen !== genRef.current) return;
      setTitle(data.title || null);
      setTodos(data.todos || []);
      setUsage(data.usage && Object.keys(data.usage).length ? data.usage : null);
      if (data.is_running) {
        // Render up to the last user message; the in-progress turn is rebuilt from replay.
        const idx = lastUserIndex(data.messages);
        setItems(groupHistory(data.messages.slice(0, idx + 1)));
        setLoading(false);
        setScrollNonce((n) => n + 1);
        await attachRef.current(id, gen);
      } else {
        setItems(groupHistory(data.messages));
        setScrollNonce((n) => n + 1);
      }
    } catch (e) {
      if (gen !== genRef.current) return;
      if (e instanceof api.ApiError && e.status === 404) {
        cbRef.current.toastError('That chat no longer exists.');
        cbRef.current.onNavigateSession(null);
      } else {
        setLoadError(api.errorMessage(e));
      }
    } finally {
      if (gen === genRef.current) setLoading(false);
    }
  }, []);

  const finalize = useCallback(
    (outcome: StreamOutcome, gen: number) => {
      if (gen !== genRef.current) return;
      cancelFlush();
      setReconnecting(false);
      const turn = liveRef.current;
      if (turn) {
        let final = turn;
        if (turn.status === 'streaming') {
          if (outcome === 'lost') final = { ...finishTurn(turn, 'error'), error: turn.error || 'Connection to the server was lost.' };
          else final = finishTurn(turn, turn.error ? 'error' : turn.blocks.length ? 'done' : 'interrupted');
        }
        if (final.blocks.length > 0 || final.error) {
          const req = currentReqRef.current;
          if (req) requestsRef.current.set(final.key, req);
          setItems((prev) => [...prev, { kind: 'assistant', key: final.key, turn: final }]);
          // Only replies the user just asked for in this tab, not re-attached or reloaded ones.
          if (req && final.status === 'done' && outcome !== 'lost') {
            maybeAutoSpeak(final.key, turnText(final), cbRef.current.toastError);
          }
        }
      }
      liveRef.current = null;
      setLive(null);
      setRunningBoth(false);
      currentReqRef.current = null;

      const sid = sidRef.current;
      if (sid) {
        cbRef.current.onSessionsChanged();
        // Titles are generated asynchronously after the first turn.
        setTimeout(() => cbRef.current.onSessionsChanged(), 4000);
      }
      const nav = pendingNavRef.current;
      pendingNavRef.current = null;
      if (nav) {
        cbRef.current.onNavigateSession(nav);
        return;
      }
      if (sid && (idleRef.current || outcome === 'stale' || outcome === 'lost')) {
        idleRef.current = false;
        if (outcome === 'lost') cbRef.current.toastError('Lost connection to the server; reloaded the conversation.');
        load(sid, gen);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [load],
  );

  attachRef.current = async (id: string, gen: number) => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    idleRef.current = false;
    currentReqRef.current = null;
    const turn = newTurn();
    liveRef.current = turn;
    setLive(turn);
    setRunningBoth(true);
    try {
      const outcome = await attachSessionStream(id, 0, {
        signal: ctrl.signal,
        onEvent: (ev) => handleEvent(ev, gen),
        onReconnecting: () => gen === genRef.current && setReconnecting(true),
        onReconnected: () => gen === genRef.current && setReconnecting(false),
      });
      finalize(outcome, gen);
    } catch (e) {
      if (gen !== genRef.current) return;
      if (liveRef.current) liveRef.current = { ...liveRef.current, error: api.errorMessage(e) };
      finalize('lost', gen);
    }
  };

  // ─── Session switching ────────────────────────────────────────
  useEffect(() => {
    const nonceChanged = nonceRef.current !== newChatNonce;
    nonceRef.current = newChatNonce;
    if (sessionId && sessionId === sidRef.current && !nonceChanged) return; // our own new session
    if (!sessionId && !sidRef.current && !nonceChanged && genRef.current > 0) return;

    abortRef.current?.abort();
    abortRef.current = null;
    cancelFlush();
    const gen = ++genRef.current;
    sidRef.current = sessionId;
    liveRef.current = null;
    currentReqRef.current = null;
    pendingNavRef.current = null;
    idleRef.current = false;
    requestsRef.current.clear();
    setLive(null);
    runningRef.current = false;
    setRunning(false);
    setStopping(false);
    setReconnecting(false);
    setItems([]);
    setTodos([]);
    setUsage(null);
    setTitle(null);
    setLoadError(null);
    if (sessionId) load(sessionId, gen);
    else setLoading(false);
  }, [sessionId, newChatNonce, load]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    },
    [],
  );

  // ─── Sending ──────────────────────────────────────────────────
  const send = useCallback(
    async ({ text, uploads = [], editMessageId, regenerate }: SendOptions): Promise<boolean> => {
      if (runningRef.current) {
        cbRef.current.toastInfo('Alita is still working on the previous message.');
        return false;
      }
      const gen = genRef.current;
      const sid = sidRef.current;
      if ((regenerate || editMessageId != null) && !sid) return false;

      const name = text.startsWith('/') ? text.slice(1).split(/\s/)[0].toLowerCase() : '';
      const isCommand = !!name && uploads.length === 0 && commandsRef.current.some((c) => c.name === name);

      let userKey: string | null = null;
      if (!regenerate) {
        const userItem: UserItem = {
          kind: 'user',
          key: uid('user'),
          dbId: null,
          text,
          attachments: uploads.filter((u) => u.kind !== 'image').map((u) => ({ name: u.name, kind: u.kind })),
          images: uploads.filter((u) => u.kind === 'image').map((u) => ({ url: u.url, name: u.name })),
          createdAt: new Date().toISOString(),
        };
        userKey = userItem.key;
        if (editMessageId != null) {
          setItems((prev) => {
            const idx = prev.findIndex((it) => it.kind === 'user' && it.dbId === editMessageId);
            return [...(idx >= 0 ? prev.slice(0, idx) : prev), userItem];
          });
        } else {
          setItems((prev) => [...prev, userItem]);
        }
      } else {
        setItems((prev) => {
          let idx = -1;
          prev.forEach((it, i) => {
            if (it.kind === 'user') idx = i;
          });
          return idx >= 0 ? prev.slice(0, idx + 1) : prev;
        });
      }

      const req: PendingRequest = { text, uploads, userKey };
      currentReqRef.current = req;
      idleRef.current = false;
      const turn = newTurn();
      liveRef.current = isCommand ? null : turn;
      setLive(isCommand ? null : turn);
      setRunningBoth(true);
      setScrollNonce((n) => n + 1);

      const body: ChatRequest = { message: text, session_id: sid };
      if (uploads.length) body.attachments = uploads.map((u) => u.id);
      if (editMessageId != null) body.edit_message_id = editMessageId;
      if (regenerate) body.regenerate = true;

      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      try {
        const outcome = await startChatStream(body, {
          signal: ctrl.signal,
          onEvent: (ev) => handleEvent(ev, gen),
          onReconnecting: () => gen === genRef.current && setReconnecting(true),
          onReconnected: () => gen === genRef.current && setReconnecting(false),
        });
        finalize(outcome, gen);
      } catch (e) {
        if (gen !== genRef.current) return true;
        const status = e instanceof api.ApiError ? e.status : 0;
        if (status === 409 && sid) {
          // Already running: drop the optimistic message and follow the live turn.
          setItems((prev) => prev.filter((it) => it.key !== userKey));
          liveRef.current = null;
          setLive(null);
          setRunningBoth(false);
          currentReqRef.current = null;
          cbRef.current.toastInfo('This chat is already working on a response; reconnecting.');
          load(sid, gen);
          return false;
        }
        if (status === 404 && sid) {
          setItems((prev) => prev.filter((it) => it.key !== userKey));
          liveRef.current = null;
          setLive(null);
          setRunningBoth(false);
          cbRef.current.toastError('This chat no longer exists.');
          cbRef.current.onNavigateSession(null);
          return false;
        }
        liveRef.current = { ...(liveRef.current || newTurn()), error: api.errorMessage(e), status: 'error' };
        finalize('ended', gen);
      }
      return true;
    },
    [handleEvent, finalize, load],
  );

  const stop = useCallback(async () => {
    if (!runningRef.current) return;
    const sid = sidRef.current;
    setStopping(true);
    if (!sid) {
      abortRef.current?.abort();
      if (liveRef.current) liveRef.current = finishTurn(liveRef.current, 'interrupted');
      finalize('ended', genRef.current);
      return;
    }
    try {
      await api.interruptSession(sid);
    } catch (e) {
      setStopping(false);
      cbRef.current.toastError(`Could not stop: ${api.errorMessage(e)}`);
    }
  }, [finalize]);

  const approve = useCallback(async (approvalId: string, approved: boolean) => {
    try {
      await api.resolveApproval(approvalId, approved);
    } catch (e) {
      cbRef.current.toastError(`Approval failed: ${api.errorMessage(e)}`);
    }
  }, []);

  const regenerate = useCallback(() => send({ text: '', regenerate: true }), [send]);

  const edit = useCallback(
    (item: UserItem, text: string) => {
      if (item.dbId == null) return;
      const uploads: UploadResult[] = [];
      for (const img of item.images) {
        const id = uploadIdFromUrl(img.url);
        if (id) uploads.push({ id, name: img.name, mime: 'image/*', size: 0, kind: 'image', url: img.url });
      }
      send({ text, uploads, editMessageId: item.dbId });
    },
    [send],
  );

  const retry = useCallback(
    (turnKey: string) => {
      const item = items.find((it) => it.kind === 'assistant' && it.turn.key === turnKey);
      if (!item || item.kind !== 'assistant') return;
      const req = requestsRef.current.get(turnKey);
      if (item.turn.started || !req || req.userKey === null) {
        send({ text: '', regenerate: true });
        return;
      }
      // The server never saw the message: drop the failed exchange and resend it.
      setItems((prev) => prev.filter((it) => it.key !== turnKey && it.key !== req.userKey));
      send({ text: req.text, uploads: req.uploads });
    },
    [items, send],
  );

  const reload = useCallback(() => {
    const sid = sidRef.current;
    if (sid && !runningRef.current) load(sid, genRef.current);
  }, [load]);

  return {
    items,
    live,
    running,
    stopping,
    loading,
    loadError,
    todos,
    usage,
    title,
    reconnecting,
    scrollNonce,
    send,
    stop,
    approve,
    regenerate,
    edit,
    retry,
    reload,
    sessionId: sidRef.current,
  };
}
