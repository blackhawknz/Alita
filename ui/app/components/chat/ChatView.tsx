'use client';

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type DragEvent } from 'react';
import { ArrowDown, CircleAlert, Info, RefreshCw, Upload, WifiOff } from 'lucide-react';
import * as api from '@/lib/api';
import { useVoiceConversation } from '@/lib/conversation';
import { cx, downloadText, safeFilename } from '@/lib/format';
import { turnText } from '@/lib/turns';
import type { Health, SessionSummary, SlashCommand } from '@/lib/types';
import { enqueueSpeech, markStreamedSpeech, nextSpeechChunk, speechText, stopSpeaking } from '@/lib/voice';
import type { SessionActions } from '../AppShell';
import { useToast } from '../Toaster';
import { CenteredSpinner } from '../ui';
import AssistantMessage from './AssistantMessage';
import ChatEmpty from './ChatEmpty';
import ChatHeader from './ChatHeader';
import Composer, { type ComposerHandle } from './Composer';
import Markdown from './Markdown';
import PlanWidget from './PlanWidget';
import { Divider } from './TurnBlocks';
import UserMessage from './UserMessage';
import { useChat } from './useChat';

interface Props {
  sessionId: string | null;
  session: SessionSummary | null;
  newChatNonce: number;
  health: Health | null;
  commands: SlashCommand[];
  isMobile: boolean;
  onOpenSidebar: () => void;
  onSessionCreated: (id: string, title?: string) => void;
  onSessionsChanged: () => void;
  onTitle: (id: string, title: string) => void;
  onRunningChange: (id: string, running: boolean) => void;
  onNavigateSession: (id: string | null) => void;
  actions: SessionActions;
}

export default function ChatView(props: Props) {
  const { sessionId, session, newChatNonce, health, commands, actions } = props;
  const toast = useToast();
  const composerRef = useRef<ComposerHandle>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const [showJump, setShowJump] = useState(false);
  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);

  const chat = useChat(sessionId, newChatNonce, commands, {
    onSessionCreated: props.onSessionCreated,
    onSessionsChanged: props.onSessionsChanged,
    onTitle: props.onTitle,
    onRunningChange: props.onRunningChange,
    onNavigateSession: props.onNavigateSession,
    toastError: toast.error,
    toastInfo: toast.info,
  });

  const chatSend = useRef(chat.send);
  chatSend.current = chat.send;
  const chatSession = useRef<string | null>(null);
  chatSession.current = chat.sessionId || sessionId;
  const pendingVoice = useRef<{ sessionId: string | null; text: string } | null>(null);
  const voice = useVoiceConversation({
    running: chat.running,
    onUtterance: (text) => {
      if (chat.running) {
        pendingVoice.current = { sessionId: chatSession.current, text };
        if (chatSession.current) void api.interruptSession(chatSession.current);
        return;
      }
      void chatSend.current({ text });
    },
    onInterrupt: () => {
      if (chatSession.current) void api.interruptSession(chatSession.current);
    },
    onError: toast.error,
  });
  useEffect(() => {
    if (chat.running || !pendingVoice.current) return;
    const pending = pendingVoice.current;
    pendingVoice.current = null;
    if (pending.sessionId === chatSession.current) void chatSend.current({ text: pending.text });
  }, [chat.running]);
  const speechCursor = useRef<{ key: string | null; offset: number }>({ key: null, offset: 0 });
  const speechMuted = useRef<string | null>(null);
  const stopSpeech = useCallback(() => {
    if (speechCursor.current.key) speechMuted.current = speechCursor.current.key;
    stopSpeaking();
  }, []);

  // Speak complete sentences while the reply is still streaming. This removes
  // the long wait between finishing a spoken turn and hearing the first word.
  useEffect(() => {
    if (!voice.enabled) {
      speechCursor.current = { key: null, offset: 0 };
      speechMuted.current = null;
      return;
    }
    const live = chat.live;
    let turn = live;
    if (!turn) {
      const last = chat.items[chat.items.length - 1];
      if (last?.kind === 'assistant' && speechCursor.current.key === last.turn.key) turn = last.turn;
    }
    if (!turn) {
      if (speechCursor.current.key) {
        stopSpeaking();
        speechCursor.current = { key: null, offset: 0 };
        speechMuted.current = null;
      }
      return;
    }
    if (speechCursor.current.key !== turn.key) {
      if (!live) return;
      if (speechCursor.current.key) stopSpeaking();
      speechCursor.current = { key: turn.key, offset: 0 };
    }
    if (speechMuted.current && speechMuted.current !== turn.key) speechMuted.current = null;
    const raw = speechText(turnText(turn));
    if (!raw) return;
    if (speechMuted.current === turn.key) {
      speechCursor.current.offset = raw.length;
      return;
    }
    markStreamedSpeech(turn.key);
    let offset = Math.min(speechCursor.current.offset, raw.length);
    const final = !live || turn.status !== 'streaming';
    while (offset < raw.length) {
      const part = nextSpeechChunk(raw.slice(offset), final);
      if (!part.text || part.consumed <= 0) break;
      offset += part.consumed;
      enqueueSpeech(turn.key, part.text, toast.error);
    }
    speechCursor.current.offset = offset;
  }, [chat.live, chat.items, voice.enabled, toast.error]);

  const activeId = chat.sessionId || sessionId;
  const title = session?.title || chat.title || (activeId ? 'Untitled chat' : 'New chat');

  // ─── Scrolling ────────────────────────────────────────────────
  const scrollToBottom = useCallback((smooth = false) => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
  }, []);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    atBottomRef.current = atBottom;
    setShowJump(!atBottom);
  };

  useLayoutEffect(() => {
    if (atBottomRef.current) scrollToBottom();
  }, [chat.items, chat.live, scrollToBottom]);

  useLayoutEffect(() => {
    atBottomRef.current = true;
    setShowJump(false);
    scrollToBottom();
  }, [chat.scrollNonce, scrollToBottom]);

  // Keep pinned to the bottom when content grows (images loading, code blocks expanding).
  useEffect(() => {
    const el = contentRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => {
      if (atBottomRef.current) scrollToBottom();
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [scrollToBottom]);

  // ─── Session actions from the header ──────────────────────────
  const exportChat = async () => {
    if (!activeId) return;
    try {
      const md = await api.exportSession(activeId);
      downloadText(`${safeFilename(title)}.md`, md);
    } catch (e) {
      toast.error(`Export failed: ${api.errorMessage(e)}`);
    }
  };

  const compact = async () => {
    if (!activeId) return;
    toast.info('Compacting context…');
    try {
      const res = await api.compactSession(activeId);
      if (res.success) {
        toast.success('Older turns summarised to free up context.');
        chat.reload();
      } else {
        toast.info('Nothing to compact yet.');
      }
    } catch (e) {
      toast.error(`Compaction failed: ${api.errorMessage(e)}`);
    }
  };

  // ─── Drag and drop ────────────────────────────────────────────
  const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types || []).includes('Files');
  const onDragEnter = (e: DragEvent) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth.current++;
    setDragging(true);
  };
  const onDragLeave = (e: DragEvent) => {
    if (!hasFiles(e)) return;
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragging(false);
  };
  const onDrop = (e: DragEvent) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    if (e.dataTransfer.files?.length) composerRef.current?.addFiles(e.dataTransfer.files);
  };

  const lastAssistantKey = useMemo(() => {
    if (chat.live) return null;
    const last = chat.items[chat.items.length - 1];
    return last && last.kind === 'assistant' ? last.key : null;
  }, [chat.items, chat.live]);

  const empty = !chat.loading && !chat.loadError && chat.items.length === 0 && !chat.live;

  return (
    <div
      className="relative flex h-full min-w-0 flex-col"
      onDragEnter={onDragEnter}
      onDragOver={(e) => hasFiles(e) && e.preventDefault()}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      <ChatHeader
        title={title}
        sessionId={activeId}
        pinned={!!session?.pinned}
        running={chat.running}
        health={health}
        usage={chat.usage}
        isMobile={props.isMobile}
        onOpenSidebar={props.onOpenSidebar}
        onRename={(t) => activeId && actions.rename(activeId, t)}
        onTogglePin={() => activeId && actions.togglePin(activeId, !session?.pinned)}
        onExport={exportChat}
        onCompact={compact}
        onDelete={() => activeId && actions.remove(activeId)}
        voice={voice}
      />

      {chat.reconnecting && (
        <div className="flex flex-none items-center justify-center gap-2 border-b border-warn/20 bg-warn/[0.06] py-1.5 text-xs text-warn">
          <WifiOff className="h-3.5 w-3.5" /> Connection interrupted, reconnecting…
        </div>
      )}

      <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
        <div ref={contentRef} className={cx('mx-auto w-full max-w-3xl px-4 md:px-6', empty ? 'h-full' : 'pb-6 pt-6')}>
          {chat.loading && chat.items.length === 0 && <div className="h-64"><CenteredSpinner label="Loading conversation…" /></div>}

          {chat.loadError && (
            <div className="mx-auto mt-16 max-w-md rounded-xl border border-danger/30 bg-danger/[0.06] p-4 text-center">
              <CircleAlert className="mx-auto h-6 w-6 text-danger" />
              <p className="mt-2 text-sm font-medium text-fg">Could not load this chat</p>
              <p className="mt-1 text-xs text-muted">{chat.loadError}</p>
              <button className="btn-secondary mt-3" onClick={chat.reload}>
                <RefreshCw className="h-3.5 w-3.5" /> Try again
              </button>
            </div>
          )}

          {empty && <ChatEmpty onPick={(p) => composerRef.current?.setText(p)} />}

          <div className="space-y-7">
            {chat.items.map((item) => {
              switch (item.kind) {
                case 'user':
                  return (
                    <UserMessage key={item.key} item={item} canEdit={!chat.running && item.dbId != null && !!activeId} onEdit={chat.edit} />
                  );
                case 'assistant':
                  return (
                    <AssistantMessage
                      key={item.key}
                      turn={item.turn}
                      isLast={item.key === lastAssistantKey}
                      busy={chat.running}
                      onApprove={chat.approve}
                      onRegenerate={activeId ? chat.regenerate : undefined}
                      onRetry={chat.retry}
                    />
                  );
                case 'divider':
                  return <Divider key={item.key} text={item.text} />;
                case 'command':
                  return (
                    <div key={item.key} className="animate-fade-in">
                      <div
                        className={cx(
                          'rounded-xl border px-4 py-3',
                          item.level === 'error' ? 'border-danger/30 bg-danger/[0.05]' : 'border-line-strong bg-surface',
                        )}
                      >
                        <div className="mb-1.5 flex items-center gap-2 text-[11.5px] text-muted">
                          {item.level === 'error' ? <CircleAlert className="h-3.5 w-3.5 text-danger" /> : <Info className="h-3.5 w-3.5 text-accent" />}
                          <span className="font-mono text-primary">{item.input}</span>
                          <span className="ml-auto text-[10.5px] text-muted-2">only visible to you</span>
                        </div>
                        <Markdown text={item.message || '_(no output)_'} className="md-sm" />
                      </div>
                    </div>
                  );
                default:
                  return null;
              }
            })}
            {chat.live && (
              <AssistantMessage
                key={chat.live.key}
                turn={chat.live}
                isLast
                busy
                onApprove={chat.approve}
              />
            )}
          </div>
        </div>
      </div>

      <div className="relative flex-none px-4 pb-4 pt-2 md:px-6">
        {showJump && !empty && (
          <button
            onClick={() => {
              atBottomRef.current = true;
              setShowJump(false);
              scrollToBottom(true);
            }}
            className="absolute -top-11 left-1/2 z-10 flex h-8 w-8 -translate-x-1/2 items-center justify-center rounded-full border border-line-strong bg-overlay text-fg-2 shadow-lg transition-colors hover:text-fg"
            title="Jump to latest"
            aria-label="Jump to latest"
          >
            <ArrowDown className="h-4 w-4" />
          </button>
        )}
        <div className="mx-auto w-full max-w-3xl">
          {voice.enabled && (
            <div
              className={cx(
                'mb-2 flex items-center gap-2 rounded-xl border px-3 py-2 text-[12px]',
                voice.phase === 'error'
                  ? 'border-danger/30 bg-danger/[0.06] text-danger'
                  : 'border-primary/25 bg-primary/[0.05] text-fg-2',
              )}
            >
              <span
                className={cx(
                  'h-2 w-2 flex-none rounded-full',
                  voice.phase === 'listening' && 'animate-pulse bg-teal',
                  voice.phase === 'thinking' && 'animate-pulse bg-warn',
                  voice.phase === 'speaking' && 'animate-pulse bg-primary',
                  voice.phase === 'error' && 'bg-danger',
                )}
              />
              <span className="font-medium">
                {voice.phase === 'listening'
                  ? 'Listening'
                  : voice.phase === 'thinking'
                    ? 'Thinking'
                    : voice.phase === 'speaking'
                      ? 'Speaking'
                      : voice.phase === 'error'
                        ? 'Voice mode stopped'
                        : 'Voice mode'}
              </span>
              <span className="min-w-0 flex-1 truncate text-muted">
                {voice.interim || (voice.phase === 'listening' ? 'Speak naturally; pause when you are done.' : '')}
              </span>
              <button className="flex-none text-[11px] font-medium text-primary hover:underline" onClick={voice.toggle}>
                End
              </button>
            </div>
          )}
          <PlanWidget todos={chat.todos} />
          <Composer
            ref={composerRef}
            running={chat.running}
            stopping={chat.stopping}
            commands={commands}
            onSend={(text, uploads) => chat.send({ text, uploads })}
            onStop={() => {
              stopSpeech();
              chat.stop();
            }}
            onError={toast.error}
            autoFocusKey={`${sessionId ?? 'new'}-${newChatNonce}`}
            voiceSupported={voice.supported}
            voicePhase={voice.phase}
            pushToTalk={voice.settings.pushToTalk}
            onPushToTalkStart={voice.startPushToTalk}
            onPushToTalkStop={voice.stopPushToTalk}
          />
          <p className="mt-2 text-center text-[11px] text-muted-2">
            Alita can make mistakes and can act on your machine. Review important actions.
          </p>
        </div>
      </div>

      {dragging && (
        <div className="pointer-events-none absolute inset-0 z-30 flex items-center justify-center bg-bg/80 backdrop-blur-sm animate-fade-in">
          <div className="flex flex-col items-center gap-3 rounded-2xl border-2 border-dashed border-primary/50 px-12 py-10">
            <Upload className="h-8 w-8 text-primary" />
            <p className="text-sm font-medium text-fg">Drop files to attach</p>
            <p className="text-xs text-muted">Images, text and documents up to 25 MB</p>
          </div>
        </div>
      )}
    </div>
  );
}
