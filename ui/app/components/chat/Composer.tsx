'use client';

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type KeyboardEvent,
} from 'react';
import { ArrowUp, FileText, Mic, Paperclip, Square, X } from 'lucide-react';
import { MAX_UPLOAD_BYTES, errorMessage, uploadFile, withToken } from '@/lib/api';
import { cx, formatBytes } from '@/lib/format';
import type { SlashCommand, UploadResult } from '@/lib/types';
import { Spinner } from '../ui';

export interface ComposerHandle {
  addFiles: (files: FileList | File[]) => void;
  setText: (text: string) => void;
  focus: () => void;
}

interface PendingAttachment {
  localId: string;
  name: string;
  size: number;
  isImage: boolean;
  previewUrl?: string;
  progress: number;
  status: 'uploading' | 'done' | 'error';
  error?: string;
  result?: UploadResult;
}

interface Props {
  running: boolean;
  stopping: boolean;
  commands: SlashCommand[];
  onSend: (text: string, uploads: UploadResult[]) => boolean | Promise<boolean>;
  onStop: () => void;
  onError: (msg: string) => void;
  placeholder?: string;
  autoFocusKey?: string | number | null;
  voiceSupported?: boolean;
  voicePhase?: string;
  pushToTalk?: boolean;
  onPushToTalkStart?: () => void;
  onPushToTalkStop?: () => void;
}

let localCounter = 0;

const Composer = forwardRef<ComposerHandle, Props>(function Composer(
  {
    running,
    stopping,
    commands,
    onSend,
    onStop,
    onError,
    placeholder,
    autoFocusKey,
    voiceSupported = false,
    voicePhase = 'off',
    pushToTalk = false,
    onPushToTalkStart,
    onPushToTalkStop,
  },
  ref,
) {
  const [text, setText] = useState('');
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const slashRef = useRef<HTMLDivElement>(null);
  const attachmentsRef = useRef(attachments);
  attachmentsRef.current = attachments;

  const resize = useCallback(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 260)}px`;
  }, []);

  useEffect(resize, [text, resize]);

  useEffect(() => {
    taRef.current?.focus();
  }, [autoFocusKey]);

  // Revoke object URLs on unmount.
  useEffect(
    () => () => {
      attachmentsRef.current.forEach((a) => a.previewUrl && URL.revokeObjectURL(a.previewUrl));
    },
    [],
  );

  const addFiles = useCallback(
    (files: FileList | File[]) => {
      Array.from(files).forEach((file) => {
        if (file.size > MAX_UPLOAD_BYTES) {
          onError(`${file.name || 'File'} is ${formatBytes(file.size)}; the limit is 25 MB.`);
          return;
        }
        const localId = `att-${++localCounter}`;
        const isImage = file.type.startsWith('image/');
        const name = file.name || (isImage ? `pasted-image-${localCounter}.png` : `file-${localCounter}`);
        const named = file.name ? file : new File([file], name, { type: file.type });
        setAttachments((prev) => [
          ...prev,
          {
            localId,
            name,
            size: file.size,
            isImage,
            previewUrl: isImage ? URL.createObjectURL(file) : undefined,
            progress: 0,
            status: 'uploading',
          },
        ]);
        uploadFile(named, (p) =>
          setAttachments((prev) => prev.map((a) => (a.localId === localId ? { ...a, progress: p } : a))),
        )
          .then((result) =>
            setAttachments((prev) =>
              prev.map((a) => (a.localId === localId ? { ...a, status: 'done', progress: 1, result } : a)),
            ),
          )
          .catch((e) => {
            const msg = errorMessage(e);
            onError(msg);
            setAttachments((prev) => prev.map((a) => (a.localId === localId ? { ...a, status: 'error', error: msg } : a)));
          });
      });
      taRef.current?.focus();
    },
    [onError],
  );

  useImperativeHandle(
    ref,
    () => ({
      addFiles,
      setText: (t: string) => {
        setText(t);
        setTimeout(() => {
          const el = taRef.current;
          if (el) {
            el.focus();
            el.setSelectionRange(t.length, t.length);
          }
        }, 0);
      },
      focus: () => taRef.current?.focus(),
    }),
    [addFiles],
  );

  const removeAttachment = (localId: string) => {
    setAttachments((prev) => {
      const a = prev.find((x) => x.localId === localId);
      if (a?.previewUrl) URL.revokeObjectURL(a.previewUrl);
      return prev.filter((x) => x.localId !== localId);
    });
  };

  // ─── Slash autocomplete ───────────────────────────────────────
  const slashQuery = /^\/([\w-]*)$/.exec(text)?.[1];
  const slashMatches = useMemo(() => {
    if (slashQuery === undefined) return [];
    const q = slashQuery.toLowerCase();
    return commands.filter((c) => c.name.startsWith(q));
  }, [slashQuery, commands]);
  const showSlash = slashMatches.length > 0 && !slashDismissed && !(slashMatches.length === 1 && slashMatches[0].name === slashQuery);

  useEffect(() => {
    setSlashIndex(0);
    if (!text.startsWith('/')) setSlashDismissed(false);
  }, [slashQuery, text]);

  useEffect(() => {
    slashRef.current?.querySelector(`[data-slash-index="${slashIndex}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [slashIndex]);

  const pickCommand = (c: SlashCommand) => {
    const takesArgs = c.usage && c.usage.trim() !== `/${c.name}`;
    setText(`/${c.name}${takesArgs ? ' ' : ''}`);
    setSlashDismissed(true);
    taRef.current?.focus();
  };

  // ─── Send ─────────────────────────────────────────────────────
  const uploading = attachments.some((a) => a.status === 'uploading');
  const ready = attachments.filter((a) => a.status === 'done' && a.result);
  const canSend = !running && !uploading && (text.trim().length > 0 || ready.length > 0);

  const send = async () => {
    if (!canSend) return;
    const uploads = ready.map((a) => a.result!) as UploadResult[];
    const ok = await onSend(text.trim(), uploads);
    if (ok) {
      attachments.forEach((a) => a.previewUrl && URL.revokeObjectURL(a.previewUrl));
      setAttachments([]);
      setText('');
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (showSlash) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSlashIndex((i) => (i + 1) % slashMatches.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSlashIndex((i) => (i - 1 + slashMatches.length) % slashMatches.length);
        return;
      }
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
        e.preventDefault();
        pickCommand(slashMatches[slashIndex] || slashMatches[0]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setSlashDismissed(true);
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(e.clipboardData.files || []);
    if (files.length > 0) {
      e.preventDefault();
      addFiles(files);
    }
  };

  return (
    <div className="relative">
      {showSlash && (
        <div ref={slashRef} className="absolute bottom-full left-0 right-0 z-20 mb-2 max-h-72 overflow-y-auto rounded-xl border border-line-strong bg-overlay shadow-2xl shadow-black/50 animate-fade-in" role="listbox" aria-label="Slash commands">
          <div className="border-b border-line px-3 py-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-muted-2">Commands</div>
          {slashMatches.map((c, i) => (
            <button
              key={c.name}
              data-slash-index={i}
              role="option"
              aria-selected={i === slashIndex}
              onMouseDown={(e) => {
                e.preventDefault();
                pickCommand(c);
              }}
              onMouseEnter={() => setSlashIndex(i)}
              className={cx('flex w-full items-baseline gap-3 px-3 py-2 text-left', i === slashIndex ? 'bg-white/[0.06]' : '')}
            >
              <span className="flex-none font-mono text-[13px] text-primary">{c.usage || `/${c.name}`}</span>
              <span className="truncate text-xs text-muted">{c.description}</span>
            </button>
          ))}
        </div>
      )}

      <div className="rounded-2xl border border-line-strong bg-surface shadow-lg shadow-black/20 transition-colors focus-within:border-primary/45 focus-within:ring-2 focus-within:ring-primary/10">
        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 px-3 pt-3">
            {attachments.map((a) => (
              <div
                key={a.localId}
                className={cx(
                  'group/att relative flex items-center gap-2 overflow-hidden rounded-xl border bg-elevated',
                  a.status === 'error' ? 'border-danger/40' : 'border-line-strong',
                  a.isImage ? 'h-16 w-16' : 'h-12 max-w-[220px] py-1.5 pl-2 pr-7',
                )}
                title={a.error || a.name}
              >
                {a.isImage ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={a.previewUrl || withToken(a.result?.url)} alt={a.name} className="h-full w-full object-cover" />
                ) : (
                  <>
                    <div className="flex h-8 w-8 flex-none items-center justify-center rounded-lg bg-primary/10 text-primary">
                      <FileText className="h-4 w-4" />
                    </div>
                    <div className="min-w-0">
                      <p className="truncate text-xs font-medium text-fg-2">{a.name}</p>
                      <p className={cx('text-[10.5px]', a.status === 'error' ? 'text-danger' : 'text-muted')}>
                        {a.status === 'error' ? 'Upload failed' : a.status === 'uploading' ? `Uploading ${Math.round(a.progress * 100)}%` : formatBytes(a.size)}
                      </p>
                    </div>
                  </>
                )}
                {a.status === 'uploading' && a.isImage && (
                  <div className="absolute inset-0 flex items-center justify-center bg-black/55 text-[10px] font-medium text-white">
                    <Spinner className="h-4 w-4" />
                  </div>
                )}
                {a.status === 'uploading' && (
                  <div className="absolute bottom-0 left-0 h-0.5 bg-primary transition-[width]" style={{ width: `${Math.max(4, a.progress * 100)}%` }} />
                )}
                <button
                  onClick={() => removeAttachment(a.localId)}
                  className="absolute right-1 top-1 rounded-full bg-black/70 p-0.5 text-white opacity-80 transition-opacity hover:opacity-100"
                  title={`Remove ${a.name}`}
                  aria-label={`Remove ${a.name}`}
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            ))}
          </div>
        )}

        <textarea
          ref={taRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          rows={1}
          placeholder={placeholder || 'Message Alita'}
          aria-label="Message"
          className="block max-h-[260px] w-full resize-none bg-transparent px-4 pb-1 pt-3.5 text-[15px] leading-relaxed text-fg outline-none placeholder:text-muted-2"
        />

        <div className="flex items-center gap-1 px-2 pb-2">
          <input
            ref={fileRef}
            type="file"
            multiple
            className="hidden"
            onChange={(e) => {
              if (e.target.files) addFiles(e.target.files);
              e.target.value = '';
            }}
          />
          <button className="icon-btn" onClick={() => fileRef.current?.click()} title="Attach files (or drop / paste)" aria-label="Attach files">
            <Paperclip className="h-[18px] w-[18px]" />
          </button>
          <button
            className={cx('icon-btn touch-none', voicePhase === 'listening' && 'text-primary')}
            onPointerDown={
              pushToTalk
                ? (e) => {
                    e.preventDefault();
                    onPushToTalkStart?.();
                  }
                : undefined
            }
            onPointerUp={
              pushToTalk
                ? (e) => {
                    e.preventDefault();
                    onPushToTalkStop?.();
                  }
                : undefined
            }
            onPointerCancel={pushToTalk ? () => onPushToTalkStop?.() : undefined}
            onPointerLeave={
              pushToTalk
                ? (e) => {
                    if (e.buttons) onPushToTalkStop?.();
                  }
                : undefined
            }
            onClick={
              pushToTalk
                ? undefined
                : () => (voicePhase === 'listening' ? onPushToTalkStop?.() : onPushToTalkStart?.())
            }
            onContextMenu={(e) => e.preventDefault()}
            disabled={!voiceSupported || running}
            title={
              voiceSupported
                ? pushToTalk
                  ? 'Hold to talk (push-to-talk)'
                  : voicePhase === 'listening'
                    ? 'Stop listening and send'
                    : 'Click to talk'
                : 'Voice input needs Chrome or Edge'
            }
            aria-label={pushToTalk ? 'Hold to talk' : 'Toggle voice input'}
            aria-pressed={voicePhase === 'listening'}
          >
            <Mic className="h-[18px] w-[18px]" />
          </button>
          <span className="ml-1 hidden truncate text-[11px] text-muted-2 sm:inline">
            {text.length > 400 ? `${text.length.toLocaleString()} chars · ` : ''}
            <span className="kbd">Enter</span> send · <span className="kbd">Shift+Enter</span> new line · <span className="kbd">/</span> commands
          </span>
          <div className="flex-1" />
          {running ? (
            <button
              onClick={onStop}
              disabled={stopping}
              className="flex h-8 w-8 items-center justify-center rounded-full bg-fg text-bg transition-opacity hover:opacity-85 disabled:opacity-50"
              title="Stop generating"
              aria-label="Stop generating"
            >
              {stopping ? <Spinner className="h-3.5 w-3.5" /> : <Square className="h-3 w-3 fill-current" />}
            </button>
          ) : (
            <button
              onClick={send}
              disabled={!canSend}
              className="flex h-8 w-8 items-center justify-center rounded-full bg-primary-deep text-white transition-colors hover:bg-primary disabled:bg-white/10 disabled:text-muted"
              title={uploading ? 'Waiting for uploads…' : 'Send message'}
              aria-label="Send message"
            >
              {uploading ? <Spinner className="h-4 w-4" /> : <ArrowUp className="h-4 w-4" />}
            </button>
          )}
        </div>
      </div>
    </div>
  );
});

export default Composer;
