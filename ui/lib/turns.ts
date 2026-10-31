// Turn model shared by live streaming and DB history.
//
// One assistant "turn" (everything between two user messages) is rendered as
// an ordered list of blocks. `reduceTurn` builds it from stream events (live
// or replayed); `groupHistory` builds the same structure from
// `/sessions/{id}/messages` rows so both render identically.

import type {
  ImageRef,
  MessageRow,
  RecallItem,
  SkillSuggestion,
  StreamEvent,
  Todo,
} from './types';

// ─── Model ─────────────────────────────────────────────────────────

export type ToolStatus = 'running' | 'awaiting_approval' | 'success' | 'error' | 'denied' | 'interrupted';

export interface ToolApproval {
  id: string;
  reason: string;
  severity: string;
  state: 'pending' | 'approved' | 'denied';
}

export interface ToolCallView {
  id: string;
  name: string;
  params: Record<string, unknown>;
  status: ToolStatus;
  result?: unknown;
  durationMs?: number | null;
  approval?: ToolApproval;
}

export type Block =
  | { kind: 'reasoning'; key: string; text: string }
  | { kind: 'text'; key: string; text: string }
  | { kind: 'tool'; key: string; tool: ToolCallView }
  | { kind: 'recall'; key: string; items: RecallItem[] }
  | { kind: 'skills'; key: string; skills: SkillSuggestion[] }
  | { kind: 'compaction'; key: string; status: 'started' | 'done' | 'failed'; messages?: number; error?: string };

export type TurnStatus = 'streaming' | 'done' | 'interrupted' | 'error';

export interface AssistantTurn {
  key: string;
  blocks: Block[];
  status: TurnStatus;
  error: string | null;
  /** DB ids of assistant messages persisted in this turn */
  messageIds: number[];
  /** Highest event seq applied (dedupe on replay) */
  lastSeq: number;
  step: number;
  /** True once `turn_start` arrived (the user message is persisted server-side) */
  started: boolean;
  userMessageId: number | null;
  durationMs?: number;
  compacted?: boolean;
  createdAt?: string | null;
}

export interface AttachmentChip {
  name: string;
  kind: 'image' | 'text' | 'file';
  url?: string;
}

export interface UserItem {
  kind: 'user';
  key: string;
  dbId: number | null;
  text: string;
  attachments: AttachmentChip[];
  images: ImageRef[];
  createdAt: string | null;
  compacted?: boolean;
}

export interface AssistantItem {
  kind: 'assistant';
  key: string;
  turn: AssistantTurn;
}

export interface CommandItem {
  kind: 'command';
  key: string;
  input: string;
  message: string;
  level: string;
}

export interface DividerItem {
  kind: 'divider';
  key: string;
  text: string;
}

export type ChatItem = UserItem | AssistantItem | CommandItem | DividerItem;

let counter = 0;
export const uid = (prefix: string) => `${prefix}-${Date.now().toString(36)}-${(counter++).toString(36)}`;

export function newTurn(key = uid('turn')): AssistantTurn {
  return { key, blocks: [], status: 'streaming', error: null, messageIds: [], lastSeq: -1, step: 0, started: false, userMessageId: null };
}

// ─── Live reducer ──────────────────────────────────────────────────

function updateTool(turn: AssistantTurn, id: string, fn: (t: ToolCallView) => ToolCallView): AssistantTurn {
  let found = false;
  const blocks = turn.blocks.map((b) => {
    if (b.kind === 'tool' && b.tool.id === id) {
      found = true;
      return { ...b, tool: fn(b.tool) };
    }
    return b;
  });
  return found ? { ...turn, blocks } : turn;
}

function appendDelta(turn: AssistantTurn, kind: 'text' | 'reasoning', delta: string): AssistantTurn {
  if (!delta) return turn;
  const last = turn.blocks[turn.blocks.length - 1];
  if (last && last.kind === kind) {
    const blocks = turn.blocks.slice(0, -1);
    blocks.push({ ...last, text: last.text + delta });
    return { ...turn, blocks };
  }
  return { ...turn, blocks: [...turn.blocks, { kind, key: uid(kind), text: delta }] };
}

function resultIsDenied(result: unknown): boolean {
  return !!result && typeof result === 'object' && (result as Record<string, unknown>).denied === true;
}

/** Close any still-running tool cards (turn ended without their results). */
function settleTools(turn: AssistantTurn): AssistantTurn {
  if (!turn.blocks.some((b) => b.kind === 'tool' && (b.tool.status === 'running' || b.tool.status === 'awaiting_approval'))) {
    return turn;
  }
  return {
    ...turn,
    blocks: turn.blocks.map((b) =>
      b.kind === 'tool' && (b.tool.status === 'running' || b.tool.status === 'awaiting_approval')
        ? { ...b, tool: { ...b.tool, status: 'interrupted' as ToolStatus } }
        : b,
    ),
  };
}

/** Apply one stream event to a turn. Pure; events with an already-seen seq are ignored. */
export function reduceTurn(turn: AssistantTurn, ev: StreamEvent): AssistantTurn {
  if (typeof ev.seq === 'number') {
    if (ev.seq <= turn.lastSeq) return turn;
    turn = { ...turn, lastSeq: ev.seq };
  }
  switch (ev.type) {
    case 'turn_start':
      return { ...turn, started: true, userMessageId: ev.user_message_id ?? null };
    case 'step_start':
      return { ...turn, step: ev.step };
    case 'snapshot': {
      const blocks: Block[] = turn.blocks.filter((b) => b.kind !== 'text' && b.kind !== 'reasoning');
      if (ev.reasoning) blocks.push({ kind: 'reasoning', key: uid('reasoning'), text: ev.reasoning });
      if (ev.text) blocks.push({ kind: 'text', key: uid('text'), text: ev.text });
      return { ...turn, blocks, step: typeof ev.step === 'number' ? ev.step : turn.step };
    }
    case 'text':
      return appendDelta(turn, 'text', ev.content);
    case 'reasoning':
      return appendDelta(turn, 'reasoning', ev.content);
    case 'memory_recall':
      if (!ev.items?.length) return turn;
      return { ...turn, blocks: [...turn.blocks, { kind: 'recall', key: uid('recall'), items: ev.items }] };
    case 'skills_suggested':
      if (!ev.skills?.length) return turn;
      return { ...turn, blocks: [...turn.blocks, { kind: 'skills', key: uid('skills'), skills: ev.skills }] };
    case 'message':
      return typeof ev.message_id === 'number' ? { ...turn, messageIds: [...turn.messageIds, ev.message_id] } : turn;
    case 'tool_call':
      if (turn.blocks.some((b) => b.kind === 'tool' && b.tool.id === ev.id)) return turn;
      return {
        ...turn,
        blocks: [
          ...turn.blocks,
          { kind: 'tool', key: `tool-${ev.id}`, tool: { id: ev.id, name: ev.tool, params: ev.params || {}, status: 'running' } },
        ],
      };
    case 'approval_requested':
      return updateTool(turn, ev.tool_call_id, (t) => ({
        ...t,
        status: 'awaiting_approval',
        approval: { id: ev.approval_id, reason: ev.reason, severity: ev.severity, state: 'pending' },
      }));
    case 'approval':
      return updateTool(turn, ev.tool_call_id, (t) => ({
        ...t,
        status: ev.approved ? 'running' : 'denied',
        approval: t.approval
          ? { ...t.approval, state: ev.approved ? 'approved' : 'denied' }
          : { id: ev.approval_id, reason: '', severity: 'medium', state: ev.approved ? 'approved' : 'denied' },
      }));
    case 'tool_result':
      return updateTool(turn, ev.id, (t) => ({
        ...t,
        result: ev.result,
        durationMs: ev.duration_ms,
        status: ev.success ? 'success' : resultIsDenied(ev.result) ? 'denied' : 'error',
      }));
    case 'compaction': {
      const idx = turn.blocks.map((b) => b.kind).lastIndexOf('compaction');
      const block: Block = { kind: 'compaction', key: uid('compaction'), status: ev.status, messages: ev.messages, error: ev.error };
      if (ev.status !== 'started' && idx >= 0) {
        const blocks = turn.blocks.slice();
        const prev = blocks[idx] as Extract<Block, { kind: 'compaction' }>;
        blocks[idx] = { ...block, key: prev.key, messages: ev.messages ?? prev.messages };
        return { ...turn, blocks };
      }
      return { ...turn, blocks: [...turn.blocks, block] };
    }
    case 'error':
      return { ...turn, error: ev.message || 'Something went wrong.' };
    case 'done': {
      const settled = settleTools(turn);
      const error = ev.error || turn.error;
      return {
        ...settled,
        error: error || null,
        durationMs: ev.duration_ms,
        status: ev.interrupted ? 'interrupted' : error ? 'error' : 'done',
      };
    }
    default:
      return turn;
  }
}

/** Mark a turn as finished when the stream ended without a `done` event. */
export function finishTurn(turn: AssistantTurn, status: TurnStatus = 'interrupted'): AssistantTurn {
  if (turn.status !== 'streaming') return turn;
  return { ...settleTools(turn), status };
}

// ─── User text parsing ─────────────────────────────────────────────

const ATTACHMENT_BLOCK = /<attachment\s+name="([^"]*)"[^>]*>[\s\S]*?<\/attachment>/g;
const ATTACHED_LINE = /^\[Attached (image|file): (.+)\]\s*$/gm;
const CONTEXT_BLOCK = /\n*<context>[\s\S]*?<\/context>/g;

/** Strip server-appended attachment payloads from user text and return chips instead. */
export function parseUserText(raw: string): { text: string; attachments: AttachmentChip[] } {
  const attachments: AttachmentChip[] = [];
  let text = (raw || '').replace(CONTEXT_BLOCK, '');
  text = text.replace(ATTACHMENT_BLOCK, (_m, name: string) => {
    attachments.push({ name, kind: 'text' });
    return '';
  });
  text = text.replace(ATTACHED_LINE, (_m, kind: string, rest: string) => {
    const name = rest.split(/ \((?:saved at|[^)]*\bbytes\b)/)[0].trim();
    attachments.push({ name, kind: kind === 'image' ? 'image' : 'file' });
    return '';
  });
  return { text: text.replace(/\n{3,}/g, '\n\n').trim(), attachments };
}

// ─── History grouping ──────────────────────────────────────────────

/** Index of the last user row (or -1). */
export function lastUserIndex(rows: MessageRow[]): number {
  for (let i = rows.length - 1; i >= 0; i--) if (rows[i].role === 'user') return i;
  return -1;
}

/** Group DB rows into chat items (user bubbles + one assistant turn per exchange). */
export function groupHistory(rows: MessageRow[]): ChatItem[] {
  const items: ChatItem[] = [];
  let turn: AssistantTurn | null = null;
  let lastCompactedItem = -1;

  const flush = () => {
    if (turn) {
      const settled: AssistantTurn = settleTools(turn);
      items.push({ kind: 'assistant', key: settled.key, turn: settled });
      if (settled.compacted) lastCompactedItem = items.length - 1;
      turn = null;
    }
  };

  const ensureTurn = (row: MessageRow): AssistantTurn => {
    if (!turn) {
      turn = { ...newTurn(`turn-${row.id}`), status: 'done', started: true, createdAt: row.created_at };
      const prev = items[items.length - 1];
      if (prev && prev.kind === 'user') turn.userMessageId = prev.dbId;
    }
    return turn;
  };

  for (const row of rows) {
    if (row.role === 'user') {
      flush();
      const parsed = parseUserText(row.text);
      const images = row.images || [];
      items.push({
        kind: 'user',
        key: `user-${row.id}`,
        dbId: row.id,
        text: parsed.text,
        // image chips are redundant with thumbnails
        attachments: parsed.attachments.filter((a) => a.kind !== 'image' || images.length === 0),
        images,
        createdAt: row.created_at,
        compacted: row.compacted,
      });
      if (row.compacted) lastCompactedItem = items.length - 1;
    } else if (row.role === 'assistant') {
      let t = ensureTurn(row);
      if (row.reasoning) t = { ...t, blocks: [...t.blocks, { kind: 'reasoning', key: `r-${row.id}`, text: row.reasoning }] };
      if (row.text) t = { ...t, blocks: [...t.blocks, { kind: 'text', key: `t-${row.id}`, text: row.text }] };
      for (const img of row.images || []) {
        t = { ...t, blocks: [...t.blocks, { kind: 'text', key: `i-${row.id}-${img.url}`, text: `![${img.name}](${img.url})` }] };
      }
      for (const call of row.tool_calls || []) {
        t = {
          ...t,
          blocks: [
            ...t.blocks,
            { kind: 'tool', key: `tool-${call.id}`, tool: { id: call.id, name: call.name, params: call.params || {}, status: 'running' } },
          ],
        };
      }
      t = { ...t, messageIds: [...t.messageIds, row.id] };
      if (row.interrupted) t = { ...t, status: 'interrupted' };
      if (row.compacted) t = { ...t, compacted: true };
      turn = t;
    } else if (row.role === 'tool') {
      const t = ensureTurn(row);
      const status: ToolStatus = row.success ? 'success' : resultIsDenied(row.result) ? 'denied' : 'error';
      const exists = t.blocks.some((b) => b.kind === 'tool' && b.tool.id === row.tool_call_id);
      turn = exists
        ? updateTool(t, row.tool_call_id, (tool) => ({ ...tool, result: row.result, durationMs: row.duration_ms, status }))
        : {
            ...t,
            blocks: [
              ...t.blocks,
              {
                kind: 'tool',
                key: `tool-orphan-${row.id}`,
                tool: { id: row.tool_call_id || String(row.id), name: row.name, params: {}, result: row.result, durationMs: row.duration_ms, status },
              },
            ],
          };
      if (row.compacted && turn) turn = { ...turn, compacted: true };
    }
  }
  flush();

  if (lastCompactedItem >= 0 && lastCompactedItem < items.length - 1) {
    items.splice(lastCompactedItem + 1, 0, {
      kind: 'divider',
      key: 'compacted-divider',
      text: 'Earlier messages summarised',
    });
  }
  return items;
}

// ─── Helpers ───────────────────────────────────────────────────────

/** Plain text of a turn (for copy). */
export function turnText(turn: AssistantTurn): string {
  return turn.blocks
    .filter((b): b is Extract<Block, { kind: 'text' }> => b.kind === 'text')
    .map((b) => b.text)
    .join('\n\n')
    .trim();
}

/** Extract a todo list from a `todo` tool result payload. */
export function todosFromResult(result: unknown): Todo[] | null {
  if (!result || typeof result !== 'object') return null;
  const todos = (result as { todos?: unknown }).todos;
  if (!Array.isArray(todos)) return null;
  return todos
    .filter((t): t is Record<string, unknown> => !!t && typeof t === 'object')
    .map((t) => ({ id: typeof t.id === 'number' ? t.id : undefined, title: String(t.title ?? t.content ?? ''), status: String(t.status ?? 'pending') }));
}

/** Whether any tool in the turn is still running / awaiting approval. */
export function hasActiveTool(turn: AssistantTurn): boolean {
  return turn.blocks.some((b) => b.kind === 'tool' && (b.tool.status === 'running' || b.tool.status === 'awaiting_approval'));
}
