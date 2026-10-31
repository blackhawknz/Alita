'use client';

import { memo, useState } from 'react';
import { Check, CircleAlert, Copy, RefreshCw, Square, Volume2 } from 'lucide-react';
import { copyText, cx, formatDuration } from '@/lib/format';
import { hasActiveTool, turnText, type AssistantTurn, type Block } from '@/lib/turns';
import { speak, stopSpeaking, usePlayback, useVoiceStatus } from '@/lib/voice';
import { useToast } from '../Toaster';
import { Spinner } from '../ui';
import ToolCard from './ToolCard';
import Markdown from './Markdown';
import { CompactionNotice, ReasoningBlock, RecallChip, SkillsChip } from './TurnBlocks';

export function AlitaAvatar() {
  return (
    <div className="flex h-7 w-7 flex-none items-center justify-center rounded-lg border border-line-strong bg-elevated">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src="/logo-trns.png" alt="" className="h-[18px] w-[18px] object-contain" />
    </div>
  );
}

function TypingDots({ label }: { label?: string }) {
  return (
    <div className="flex h-6 items-center gap-1.5" aria-label="Alita is working">
      {[0, 160, 320].map((d) => (
        <span key={d} className="typing-dot" style={{ animationDelay: `${d}ms` }} />
      ))}
      {label && <span className="ml-1.5 text-xs text-muted">{label}</span>}
    </div>
  );
}

/** Play / stop this reply in Alita's chosen voice. Hidden until ElevenLabs is configured. */
function SpeakButton({ turnKey, text }: { turnKey: string; text: string }) {
  const voice = useVoiceStatus();
  const state = usePlayback(turnKey);
  const toast = useToast();
  if (!voice?.configured) return null;
  if (!voice.voice) {
    return (
      <button
        className="icon-btn h-7 w-7 cursor-not-allowed opacity-50"
        title="Alita hasn't chosen a voice yet. Ask her to pick one."
        aria-label="Alita hasn't chosen a voice yet"
        aria-disabled="true"
        onClick={() => toast.info("Alita hasn't chosen a voice yet. Ask her to pick one.")}
      >
        <Volume2 className="h-3.5 w-3.5" />
      </button>
    );
  }
  const active = state !== 'idle';
  const label = active ? 'Stop speaking' : `Read aloud (${voice.voice.name})`;
  return (
    <button
      className={cx('icon-btn h-7 w-7', active && 'text-primary')}
      onClick={() => (active ? stopSpeaking() : speak(turnKey, text, toast.error))}
      title={label}
      aria-label={label}
      aria-pressed={active}
    >
      {state === 'loading' ? (
        <Spinner className="h-3.5 w-3.5" />
      ) : state === 'playing' ? (
        <Square className="h-3 w-3 fill-current" />
      ) : (
        <Volume2 className="h-3.5 w-3.5" />
      )}
    </button>
  );
}

/** Group consecutive tool blocks so they stack tightly. */
function segment(blocks: Block[]): (Block | Block[])[] {
  const out: (Block | Block[])[] = [];
  for (const b of blocks) {
    const last = out[out.length - 1];
    if (b.kind === 'tool' && Array.isArray(last)) last.push(b);
    else if (b.kind === 'tool') out.push([b]);
    else out.push(b);
  }
  return out;
}

function AssistantMessage({
  turn,
  isLast,
  busy,
  onApprove,
  onRegenerate,
  onRetry,
}: {
  turn: AssistantTurn;
  isLast: boolean;
  busy: boolean;
  onApprove: (approvalId: string, approved: boolean) => Promise<void>;
  onRegenerate?: () => void;
  onRetry?: (turnKey: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  const streaming = turn.status === 'streaming';
  const segments = segment(turn.blocks);
  const lastBlock = turn.blocks[turn.blocks.length - 1];
  const text = turnText(turn);
  const speaking = usePlayback(turn.key) !== 'idle';

  const showWorking =
    streaming &&
    (turn.blocks.length === 0 ||
      (lastBlock && lastBlock.kind !== 'text' && lastBlock.kind !== 'reasoning' && !hasActiveTool(turn)));

  const copy = async () => {
    if (await copyText(text)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <div className="group/turn flex gap-3.5 animate-fade-in">
      <AlitaAvatar />
      <div className="min-w-0 flex-1 space-y-3 pt-0.5">
        {segments.map((seg, i) => {
          if (Array.isArray(seg)) {
            return (
              <div key={seg[0].key} className="space-y-1.5">
                {seg.map((b) => (b.kind === 'tool' ? <ToolCard key={b.key} tool={b.tool} onApprove={onApprove} /> : null))}
              </div>
            );
          }
          const isLastSeg = i === segments.length - 1;
          switch (seg.kind) {
            case 'text':
              return (
                <Markdown key={seg.key} text={seg.text} className={cx(streaming && isLastSeg && 'streaming-cursor')} />
              );
            case 'reasoning':
              return <ReasoningBlock key={seg.key} text={seg.text} active={streaming && isLastSeg} />;
            case 'recall':
              return <RecallChip key={seg.key} items={seg.items} />;
            case 'skills':
              return <SkillsChip key={seg.key} skills={seg.skills} />;
            case 'compaction':
              return <CompactionNotice key={seg.key} status={seg.status} messages={seg.messages} error={seg.error} />;
            default:
              return null;
          }
        })}

        {showWorking && <TypingDots label={turn.blocks.length === 0 ? undefined : 'Thinking…'} />}

        {turn.error && (
          <div className="flex items-start gap-2.5 rounded-xl border border-danger/30 bg-danger/[0.07] px-3.5 py-2.5">
            <CircleAlert className="mt-0.5 h-4 w-4 flex-none text-danger" />
            <div className="min-w-0 flex-1">
              <p className="text-[13px] font-medium text-danger">Something went wrong</p>
              <p className="mt-0.5 whitespace-pre-wrap break-words text-[12.5px] leading-relaxed text-fg-2">{turn.error}</p>
            </div>
            {isLast && !busy && onRetry && (
              <button className="btn-secondary flex-none px-2.5 py-1 text-xs" onClick={() => onRetry(turn.key)}>
                <RefreshCw className="h-3.5 w-3.5" /> Retry
              </button>
            )}
          </div>
        )}

        {!streaming && (
          <div
            className={cx(
              'flex items-center gap-0.5 text-muted transition-opacity',
              isLast || speaking ? 'opacity-100' : 'opacity-0 focus-within:opacity-100 group-hover/turn:opacity-100',
            )}
          >
            {text && (
              <button className="icon-btn h-7 w-7" onClick={copy} title="Copy response" aria-label="Copy response">
                {copied ? <Check className="h-3.5 w-3.5 text-teal" /> : <Copy className="h-3.5 w-3.5" />}
              </button>
            )}
            {text && <SpeakButton turnKey={turn.key} text={text} />}
            {isLast && onRegenerate && !busy && (
              <button className="icon-btn h-7 w-7" onClick={onRegenerate} title="Regenerate response" aria-label="Regenerate response">
                <RefreshCw className="h-3.5 w-3.5" />
              </button>
            )}
            {turn.status === 'interrupted' && (
              <span className="ml-1.5 flex items-center gap-1 text-[11px] text-muted">
                <Square className="h-2.5 w-2.5 fill-current" /> Stopped
              </span>
            )}
            {turn.durationMs != null && turn.durationMs > 0 && (
              <span className="ml-1.5 font-mono text-[10.5px] text-muted-2">{formatDuration(turn.durationMs)}</span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default memo(AssistantMessage);
