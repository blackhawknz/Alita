// API client for the Alita backend. Every request goes through the Next
// rewrite `/api/:path*` -> backend.

import type {
  ActivityResponse,
  ActivityRunDetail,
  ChatRequest,
  ConnectionTest,
  GalleryImage,
  GmailStatus,
  Health,
  JobRun,
  KanbanCard,
  LiquidModelStatus,
  MemoryEntry,
  MemoryOverview,
  PendingApproval,
  Personality,
  PreparedSpeech,
  SystemPromptPreview,
  ScheduledJob,
  SearchResult,
  SessionDetail,
  SessionMessages,
  SessionSummary,
  Settings,
  SettingsUpdate,
  Skill,
  SkillDetail,
  SlashCommand,
  Stats,
  ToolsResponse,
  UploadResult,
  Voice,
  VoiceInfo,
  VoiceStatus,
} from './types';

export const API_BASE = '/api';

// Optional bearer token mirroring the backend's ALITA_API_TOKEN.
const AUTH_TOKEN = process.env.NEXT_PUBLIC_ALITA_TOKEN || '';

export function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const headers = { ...extra };
  if (AUTH_TOKEN) headers['Authorization'] = `Bearer ${AUTH_TOKEN}`;
  return headers;
}

/** Append `?token=` to backend URLs used in <img src> / links (no headers there). */
export function withToken(url: string | undefined | null): string {
  if (!url) return '';
  if (!AUTH_TOKEN || !url.startsWith(`${API_BASE}/`)) return url;
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(AUTH_TOKEN)}`;
}

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail || `Request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

/** Extract FastAPI's `{detail}` (string or validation list) from an error response. */
export async function errorFromResponse(res: Response): Promise<ApiError> {
  let detail = res.statusText;
  try {
    const text = await res.text();
    try {
      const data = JSON.parse(text);
      if (typeof data?.detail === 'string') detail = data.detail;
      else if (Array.isArray(data?.detail)) detail = data.detail.map((d: { msg?: string }) => d.msg).join('; ');
      else if (text) detail = text;
    } catch {
      if (text) detail = text.slice(0, 300);
    }
  } catch {
    /* ignore */
  }
  if (res.status === 502 || res.status === 504 || (res.status === 500 && /ECONNREFUSED|socket hang up/i.test(detail))) {
    detail = 'Backend unreachable. Is the Alita server running on port 8080?';
  }
  return new ApiError(res.status, detail);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: authHeaders((init.headers as Record<string, string>) || {}),
    });
  } catch {
    throw new ApiError(0, 'Network error: could not reach the server.');
  }
  if (!res.ok) throw await errorFromResponse(res);
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) return (await res.json()) as T;
  return (await res.text()) as unknown as T;
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
});

const qs = (params: Record<string, string | number | undefined | null>) => {
  const s = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') s.set(k, String(v));
  });
  const str = s.toString();
  return str ? `?${str}` : '';
};

// ─── Health ────────────────────────────────────────────────────────

export const getHealth = () => request<Health>('/health');
export const getStats = () => request<Stats>('/stats');

// ─── Sessions ──────────────────────────────────────────────────────

export async function listSessions(q = '', limit = 200, offset = 0): Promise<SessionSummary[]> {
  const data = await request<{ sessions: SessionSummary[] }>(`/sessions${qs({ kind: 'chat', q, limit, offset })}`);
  return data.sessions || [];
}

export const createSession = (title = '') =>
  request<{ session_id: string; session: SessionDetail }>('/sessions', json('POST', { title }));

export const getSession = (id: string) => request<SessionDetail>(`/sessions/${encodeURIComponent(id)}`);

export const updateSession = (id: string, patch: { title?: string; pinned?: boolean }) =>
  request<{ success: boolean }>(`/sessions/${encodeURIComponent(id)}`, json('PATCH', patch));

export const deleteSession = (id: string) =>
  request<{ success: boolean }>(`/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });

export async function searchMessages(q: string, limit = 20): Promise<SearchResult[]> {
  const data = await request<{ results: SearchResult[] }>(`/sessions/search${qs({ q, limit })}`);
  return data.results || [];
}

export const getMessages = (id: string) => request<SessionMessages>(`/sessions/${encodeURIComponent(id)}/messages`);

export const exportSession = (id: string) =>
  request<string>(`/sessions/${encodeURIComponent(id)}/export?format=md`);

export const compactSession = (id: string) =>
  request<{ success: boolean }>(`/sessions/${encodeURIComponent(id)}/compact`, { method: 'POST' });

export const interruptSession = (id: string) =>
  request<{ success: boolean }>(`/sessions/${encodeURIComponent(id)}/interrupt`, { method: 'POST' });

// ─── Chat (streaming helpers live in lib/stream.ts) ────────────────

export function postChatStream(body: ChatRequest, signal?: AbortSignal): Promise<Response> {
  return fetch(`${API_BASE}/chat/stream`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json', Accept: 'text/event-stream' }),
    body: JSON.stringify(body),
    signal,
  });
}

export function getSessionStream(id: string, after: number, signal?: AbortSignal): Promise<Response> {
  return fetch(`${API_BASE}/sessions/${encodeURIComponent(id)}/stream?after=${after}`, {
    headers: authHeaders({ Accept: 'text/event-stream' }),
    signal,
  });
}

export async function listCommands(): Promise<SlashCommand[]> {
  const data = await request<{ commands: SlashCommand[] }>('/commands');
  return data.commands || [];
}

export const resolveApproval = (approvalId: string, decision: boolean) =>
  request<{ approval_id: string; decision: string; remaining: number }>(
    '/approve',
    json('POST', { approval_id: approvalId, decision }),
  );

export const getPendingApprovals = () =>
  request<{ pending: PendingApproval[]; count: number; mode: string }>('/approvals/pending');

// ─── Uploads ───────────────────────────────────────────────────────

export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

/** Upload a file as base64 JSON. `onProgress` reports 0..1 of the request body. */
export function uploadFile(file: File, onProgress?: (p: number) => void): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    if (file.size > MAX_UPLOAD_BYTES) {
      reject(new ApiError(413, `${file.name} is larger than 25 MB`));
      return;
    }
    const reader = new FileReader();
    reader.onerror = () => reject(new ApiError(0, `Could not read ${file.name}`));
    reader.onload = () => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `${API_BASE}/uploads`);
      xhr.setRequestHeader('Content-Type', 'application/json');
      if (AUTH_TOKEN) xhr.setRequestHeader('Authorization', `Bearer ${AUTH_TOKEN}`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
      };
      xhr.onerror = () => reject(new ApiError(0, 'Upload failed: network error'));
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText) as UploadResult);
          } catch {
            reject(new ApiError(xhr.status, 'Upload failed: invalid response'));
          }
        } else {
          let detail = xhr.statusText;
          try {
            detail = JSON.parse(xhr.responseText).detail || detail;
          } catch {
            /* ignore */
          }
          reject(new ApiError(xhr.status, `Upload failed: ${detail}`));
        }
      };
      xhr.send(
        JSON.stringify({ name: file.name || 'pasted-file', mime: file.type || undefined, data: reader.result as string }),
      );
    };
    reader.readAsDataURL(file);
  });
}

// ─── Memory ────────────────────────────────────────────────────────

export const getMemory = () => request<MemoryOverview>('/memory');

export const updateMemoryBlock = (name: string, content: string) =>
  request<{ success: boolean; name: string; chars: number }>(
    `/memory/blocks/${encodeURIComponent(name)}`,
    json('PUT', { content }),
  );

export const listMemories = (params: { q?: string; category?: string; limit?: number; offset?: number }) =>
  request<{ memories: MemoryEntry[]; total: number }>(`/memory/entries${qs(params)}`);

export const createMemory = (body: { content: string; category: string; importance: number }) =>
  request<{ status: string; id?: number }>('/memory/entries', json('POST', body));

export const updateMemory = (id: number, patch: { content?: string; category?: string; importance?: number }) =>
  request<{ success: boolean }>(`/memory/entries/${id}`, json('PATCH', patch));

export const deleteMemory = (id: number) => request<{ success: boolean }>(`/memory/entries/${id}`, { method: 'DELETE' });

// ─── Skills ────────────────────────────────────────────────────────

export async function listSkills(): Promise<Skill[]> {
  const data = await request<{ skills: Skill[] }>('/skills');
  return data.skills || [];
}

export const getSkill = (name: string) => request<SkillDetail>(`/skills/${encodeURIComponent(name)}`);

export const createSkill = (body: { name: string; description: string; content: string; tags: string[] }) =>
  request<SkillDetail>('/skills', json('POST', body));

export const updateSkill = (name: string, body: { description?: string; content?: string; tags?: string[] }) =>
  request<SkillDetail>(`/skills/${encodeURIComponent(name)}`, json('PUT', body));

export const deleteSkill = (name: string) =>
  request<{ success: boolean }>(`/skills/${encodeURIComponent(name)}`, { method: 'DELETE' });

// ─── Persona / settings ────────────────────────────────────────────

export const getPersonality = () => request<Personality>('/personality');
export const updatePersonality = (body: { persona?: string; instructions?: string }) =>
  request<Personality>('/personality', json('PUT', body));
export const resetPersonality = () => request<Personality>('/personality/reset', { method: 'POST' });
export const previewSystemPrompt = (sessionId?: string | null) =>
  request<SystemPromptPreview>(`/personality/preview${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`);

export const getSettings = () => request<Settings>('/settings');
export const updateSettings = (body: SettingsUpdate) => request<Settings>('/settings', json('PUT', body));
export const testConnection = () => request<ConnectionTest>('/settings/test', { method: 'POST' });
export const getLiquidStatus = () => request<LiquidModelStatus>('/models/liquid/status');
export const prepareLiquidModel = () => request<LiquidModelStatus>('/models/liquid/prepare', { method: 'POST' });
export const getGmailStatus = () => request<GmailStatus>('/gmail/status');

// ─── Voice (ElevenLabs) ────────────────────────────────────────────

export const getVoiceStatus = (usage = false) => request<VoiceStatus>(`/voice/status${usage ? '?usage=true' : ''}`);
export const getVoiceProviders = () =>
  request<{ active: string; providers: VoiceStatus['providers'] }>('/voice/providers');
export const getVoices = (params: {
  provider?: string;
  search?: string;
  locale?: string;
  gender?: string;
  source?: 'account' | 'library';
  limit?: number;
} = {}) => {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') qs.set(k, String(v));
  return request<{ success: boolean; provider: string; count: number; voices: VoiceInfo[]; note?: string }>(
    `/voice/voices?${qs.toString()}`,
  );
};
export const updateVoice = (body: {
  provider?: string;
  voice_id?: string;
  name?: string;
  reason?: string;
  public_owner_id?: string;
  speed?: number;
  stability?: number;
  similarity_boost?: number;
  style?: number;
  use_speaker_boost?: boolean;
  instructions?: string;
}) => request<{ success: boolean; message: string; provider: string; voice: Voice | null; status: VoiceStatus }>('/voice', json('PUT', body));
export const previewVoice = (body: { provider?: string; voice_id?: string; text?: string; public_owner_id?: string }) =>
  request<{ success: boolean; provider: string; voice: string; audio_url: string; kind: string; chars?: number }>(
    '/voice/preview',
    json('POST', body),
  );
export const prepareSpeech = (text: string) => request<PreparedSpeech>('/voice/speech', json('POST', { text }));
export const getSpeechState = (id: string) =>
  request<{ state: string; error?: string }>(`/voice/speech/${encodeURIComponent(id)}/status`);

export const getTools = () => request<ToolsResponse>('/tools');

// ─── Kanban ────────────────────────────────────────────────────────

export async function getKanbanCards(column?: string): Promise<KanbanCard[]> {
  const data = await request<{ cards: KanbanCard[] }>(`/kanban/cards${qs({ column })}`);
  return data.cards || [];
}

export async function createKanbanCard(card: {
  title: string;
  description?: string;
  column?: string;
  priority?: number;
  tags?: string[];
  auto_advance?: boolean;
  max_iterations?: number;
  executor?: KanbanCard['executor'];
  workdir?: string;
  mandate?: Record<string, unknown>;
  max_minutes?: number;
}): Promise<KanbanCard> {
  const data = await request<{ card: KanbanCard }>('/kanban/cards', json('POST', card));
  return data.card;
}

export async function updateKanbanCard(cardId: string, fields: Partial<KanbanCard>): Promise<KanbanCard> {
  const data = await request<{ card: KanbanCard }>(`/kanban/cards/${cardId}`, json('PATCH', fields));
  return data.card;
}

export const deleteKanbanCard = (cardId: string) => request<unknown>(`/kanban/cards/${cardId}`, { method: 'DELETE' });
export const retryKanbanCard = (cardId: string) => request<unknown>(`/kanban/cards/${cardId}/retry`, { method: 'POST' });
export const cancelKanbanCard = (cardId: string) =>
  request<unknown>(`/kanban/cards/${cardId}/cancel`, { method: 'POST' });

// ─── Gallery ───────────────────────────────────────────────────────

export async function getGalleryImages(): Promise<GalleryImage[]> {
  const data = await request<{ images: GalleryImage[] }>('/images');
  return data.images || [];
}

export const deleteGalleryImage = (filename: string) =>
  request<unknown>(`/images/${encodeURIComponent(filename)}`, { method: 'DELETE' });

// ─── Jobs ──────────────────────────────────────────────────────────

export async function getJobs(): Promise<ScheduledJob[]> {
  const data = await request<{ jobs: ScheduledJob[] }>('/jobs');
  return data.jobs || [];
}

export async function createJob(job: {
  name: string;
  prompt: string;
  cron_expr: string;
  description?: string;
  enabled?: boolean;
  timezone?: string;
}): Promise<ScheduledJob> {
  const data = await request<{ job: ScheduledJob }>('/jobs', json('POST', job));
  return data.job;
}

export async function updateJob(jobId: string, fields: Partial<ScheduledJob>): Promise<ScheduledJob> {
  const data = await request<{ job: ScheduledJob }>(`/jobs/${jobId}`, json('PATCH', fields));
  return data.job;
}

export const deleteJob = (jobId: string) => request<unknown>(`/jobs/${jobId}`, { method: 'DELETE' });
export const runJobNow = (jobId: string) => request<unknown>(`/jobs/${jobId}/run`, { method: 'POST' });

export async function getJobRuns(jobId: string, limit = 20): Promise<JobRun[]> {
  const data = await request<{ runs: JobRun[] }>(`/jobs/${jobId}/runs${qs({ limit })}`);
  return data.runs || [];
}

// ─── Activity ──────────────────────────────────────────────────────

export const getActivity = () => request<ActivityResponse>('/activity');

export const getActivityRun = (runId: string) =>
  request<ActivityRunDetail>(`/activity/runs/${encodeURIComponent(runId)}`);

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail || err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}
