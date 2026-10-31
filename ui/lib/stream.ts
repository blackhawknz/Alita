// SSE parsing + resilient turn streaming.
//
// `POST /chat/stream` starts a turn and streams it; `GET /sessions/{id}/stream?after=N`
// re-attaches to the session's current/last turn replaying events with seq >= N.
// If a connection drops before `stream_end`, we transparently re-attach with
// `after = lastSeq + 1` (exponential backoff, a few attempts).

import { ApiError, errorFromResponse, getSessionStream, postChatStream } from './api';
import type { ChatRequest, StreamEvent } from './types';

/** Parse a `text/event-stream` body, calling `onEvent` for every JSON `data:` payload. */
export async function readSSE(
  body: ReadableStream<Uint8Array>,
  onEvent: (ev: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let dataLines: string[] = [];

  const onAbort = () => {
    reader.cancel().catch(() => {});
  };
  signal?.addEventListener('abort', onAbort);

  const dispatch = () => {
    if (dataLines.length === 0) return;
    const payload = dataLines.join('\n');
    dataLines = [];
    try {
      onEvent(JSON.parse(payload) as StreamEvent);
    } catch {
      /* malformed JSON: skip */
    }
  };

  try {
    while (true) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() ?? '';
      for (const line of lines) {
        if (line === '') dispatch();
        else if (line.startsWith(':')) continue; // comment / keep-alive
        else if (line.startsWith('data:')) dataLines.push(line.slice(line.charAt(5) === ' ' ? 6 : 5));
        // other fields (event:, id:, retry:) are unused by the backend
      }
    }
    if (buffer.startsWith('data:')) dataLines.push(buffer.slice(buffer.charAt(5) === ' ' ? 6 : 5));
    dispatch();
  } finally {
    signal?.removeEventListener('abort', onAbort);
  }
}

/**
 * - `ended`: received `stream_end`
 * - `aborted`: the caller aborted (e.g. navigated to another chat)
 * - `stale`: on reconnect the session is running a different turn
 * - `lost`: connection dropped and could not be re-established
 */
export type StreamOutcome = 'ended' | 'aborted' | 'stale' | 'lost';

export interface StreamOptions {
  signal: AbortSignal;
  onEvent: (ev: StreamEvent) => void;
  onReconnecting?: (attempt: number) => void;
  onReconnected?: () => void;
  maxRetries?: number;
}

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => {
      clearTimeout(t);
      resolve();
    });
  });

const isAbort = (e: unknown) => e instanceof DOMException && e.name === 'AbortError';

async function runStream(
  open: () => Promise<Response>,
  sessionHint: string | null,
  opts: StreamOptions,
): Promise<StreamOutcome> {
  const { signal, onEvent, maxRetries = 5 } = opts;
  let sessionId = sessionHint;
  let turnId: string | null = null;
  let lastSeq = -1;
  let ended = false;
  let stale = false;
  let received = 0;

  const handle = (ev: StreamEvent) => {
    if (stale) return;
    if (ev.type === 'session') {
      if (ev.session_id) sessionId = ev.session_id;
      if (turnId && ev.turn_id && ev.turn_id !== turnId) {
        stale = true;
        return;
      }
      turnId = ev.turn_id || turnId;
    }
    if (typeof ev.seq === 'number') {
      if (ev.seq <= lastSeq) return; // duplicate after replay
      lastSeq = ev.seq;
    }
    received++;
    if (ev.type === 'stream_end') ended = true;
    onEvent(ev);
  };

  // Initial connection: HTTP errors are surfaced to the caller.
  let res: Response;
  try {
    res = await open();
  } catch (e) {
    if (isAbort(e) || signal.aborted) return 'aborted';
    throw new ApiError(0, 'Network error: could not reach the server.');
  }
  if (!res.ok || !res.body) throw await errorFromResponse(res);

  let attempt = 0;
  while (true) {
    const inner = new AbortController();
    const relay = () => inner.abort();
    signal.addEventListener('abort', relay);
    const before = received;
    try {
      await readSSE(res.body!, (ev) => {
        handle(ev);
        if (stale) inner.abort();
      }, inner.signal);
    } catch {
      /* dropped connection or abort: handled below */
    } finally {
      signal.removeEventListener('abort', relay);
    }
    if (signal.aborted) return 'aborted';
    if (stale) return 'stale';
    if (ended) return 'ended';
    if (!sessionId) return 'lost';
    if (received > before) attempt = 0;

    // Re-attach with backoff.
    let reopened = false;
    while (attempt < maxRetries && !reopened) {
      attempt++;
      opts.onReconnecting?.(attempt);
      await sleep(Math.min(8000, 500 * 2 ** (attempt - 1)), signal);
      if (signal.aborted) return 'aborted';
      try {
        const r = await getSessionStream(sessionId, lastSeq + 1, signal);
        if (r.status === 404) return 'lost';
        if (r.ok && r.body) {
          res = r;
          reopened = true;
          opts.onReconnected?.();
        }
      } catch (e) {
        if (isAbort(e) || signal.aborted) return 'aborted';
      }
    }
    if (!reopened) return 'lost';
  }
}

/** Start a turn via `POST /chat/stream`. Throws `ApiError` for HTTP errors (404, 409, ...). */
export function startChatStream(body: ChatRequest, opts: StreamOptions): Promise<StreamOutcome> {
  return runStream(() => postChatStream(body, opts.signal), body.session_id ?? null, opts);
}

/** Re-attach to a session's current/last turn. */
export function attachSessionStream(sessionId: string, after: number, opts: StreamOptions): Promise<StreamOutcome> {
  return runStream(() => getSessionStream(sessionId, after, opts.signal), sessionId, opts);
}
