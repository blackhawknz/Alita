'use client';

import { memo, useEffect, useRef, useState } from 'react';
import { Check, Copy, FileText, Image as ImageIcon, Paperclip, Pencil } from 'lucide-react';
import { withToken } from '@/lib/api';
import { copyText, cx, parseUtc, timeOfDay } from '@/lib/format';
import type { UserItem } from '@/lib/turns';

function UserMessage({
  item,
  canEdit,
  onEdit,
}: {
  item: UserItem;
  canEdit: boolean;
  onEdit: (item: UserItem, text: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.text);
  const [copied, setCopied] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!editing) return;
    const el = ref.current;
    if (el) {
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
      el.style.height = 'auto';
      el.style.height = `${Math.min(el.scrollHeight, 320)}px`;
    }
  }, [editing]);

  const submit = () => {
    const t = draft.trim();
    if (!t && item.images.length === 0) return;
    setEditing(false);
    onEdit(item, t);
  };

  const copy = async () => {
    if (await copyText(item.text)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };

  const droppedAttachments = item.attachments.filter((a) => a.kind !== 'image');

  if (editing) {
    return (
      <div className="ml-auto w-full max-w-[85%] animate-fade-in">
        <div className="rounded-2xl border border-primary/40 bg-elevated p-3 ring-2 ring-primary/10">
          <textarea
            ref={ref}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              e.target.style.height = 'auto';
              e.target.style.height = `${Math.min(e.target.scrollHeight, 320)}px`;
            }}
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                e.preventDefault();
                setEditing(false);
                setDraft(item.text);
              } else if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit();
              }
            }}
            aria-label="Edit message"
            className="w-full resize-none bg-transparent text-[15px] leading-relaxed text-fg outline-none"
            rows={1}
          />
          {droppedAttachments.length > 0 && (
            <p className="mt-1 text-[11px] text-warn">
              File attachments ({droppedAttachments.map((a) => a.name).join(', ')}) will not be re-sent. Images are kept.
            </p>
          )}
          <div className="mt-2 flex items-center justify-end gap-2">
            <span className="mr-auto text-[11px] text-muted">Editing removes this message and everything after it.</span>
            <button
              className="btn-ghost px-2.5 py-1 text-xs"
              onClick={() => {
                setEditing(false);
                setDraft(item.text);
              }}
            >
              Cancel
            </button>
            <button className="btn-primary px-3 py-1 text-xs" onClick={submit} disabled={!draft.trim() && item.images.length === 0}>
              Send
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="group/user flex flex-col items-end gap-1.5 animate-fade-in">
      {item.images.length > 0 && (
        <div className="flex max-w-[85%] flex-wrap justify-end gap-2">
          {item.images.map((img) => (
            <a key={img.url} href={withToken(img.url)} target="_blank" rel="noopener noreferrer" title={img.name}>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={withToken(img.url)}
                alt={img.name}
                className="h-28 max-w-[220px] rounded-xl border border-line-strong object-cover"
                loading="lazy"
              />
            </a>
          ))}
        </div>
      )}
      {item.attachments.length > 0 && (
        <div className="flex max-w-[85%] flex-wrap justify-end gap-1.5">
          {item.attachments.map((a, i) => (
            <span key={`${a.name}-${i}`} className="chip">
              {a.kind === 'image' ? <ImageIcon className="h-3 w-3" /> : a.kind === 'text' ? <FileText className="h-3 w-3" /> : <Paperclip className="h-3 w-3" />}
              <span className="max-w-[200px] truncate">{a.name}</span>
            </span>
          ))}
        </div>
      )}
      {item.text && (
        <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md border border-line-strong bg-elevated px-4 py-2.5 text-[15px] leading-relaxed text-fg">
          {item.text}
        </div>
      )}
      <div className="flex items-center gap-0.5 text-muted opacity-0 transition-opacity focus-within:opacity-100 group-hover/user:opacity-100">
        {item.createdAt && <span className="mr-1 text-[10.5px] text-muted-2">{timeOfDay(parseUtc(item.createdAt))}</span>}
        {item.text && (
          <button className="icon-btn h-7 w-7" onClick={copy} title="Copy message" aria-label="Copy message">
            {copied ? <Check className="h-3.5 w-3.5 text-teal" /> : <Copy className="h-3.5 w-3.5" />}
          </button>
        )}
        <button
          className={cx('icon-btn h-7 w-7', !canEdit && 'hidden')}
          onClick={() => {
            setDraft(item.text);
            setEditing(true);
          }}
          title="Edit message"
          aria-label="Edit message"
        >
          <Pencil className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

export default memo(UserMessage);
