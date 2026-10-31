// Types for the Alita backend API (see backend/alita/api/server.py).

// ─── Health / stats ────────────────────────────────────────────────

export interface Health {
  status: string;
  version: string;
  llm_provider: string;
  model: string;
  context_window: number;
  vector_store: boolean;
  host_exec_allowed: boolean;
  running_sessions: string[];
}

export interface MemoryStats {
  total: number;
  by_category: Record<string, number>;
  semantic_search: boolean;
}

export interface Stats {
  sessions: number;
  memories: MemoryStats;
  skills: number;
  tools: number;
  running: Array<{ id: string; title: string; kind: string }>;
}

// ─── Sessions ──────────────────────────────────────────────────────

export interface SessionSummary {
  id: string;
  title: string;
  kind: string;
  pinned: boolean;
  created_at: string;
  updated_at: string;
  message_count: number;
  is_running: boolean;
  compacted: boolean;
  parent_id: string | null;
}

export type TodoStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled';

export interface Todo {
  id?: number;
  title: string;
  status: TodoStatus | string;
}

export interface Usage {
  context_tokens?: number;
  completion_tokens?: number;
  cached_tokens?: number;
  context_window?: number;
}

export interface SessionDetail extends SessionSummary {
  turn_id: string | null;
  todos: Todo[];
  usage: Usage;
  context_summary: string;
}

export interface SearchResult {
  session_id: string;
  title: string | null;
  role: string;
  snippet: string | null;
  created_at: string;
  message_id: number;
}

export interface ImageRef {
  url: string;
  name: string;
}

export interface ApiToolCall {
  id: string;
  name: string;
  params: Record<string, unknown>;
}

export interface UserRow {
  id: number;
  role: 'user';
  text: string;
  images: ImageRef[];
  created_at: string;
  compacted?: boolean;
}

export interface AssistantRow {
  id: number;
  role: 'assistant';
  text: string;
  images: ImageRef[];
  tool_calls?: ApiToolCall[];
  reasoning?: string;
  interrupted?: boolean;
  created_at: string;
  compacted?: boolean;
}

export interface ToolRow {
  id: number;
  role: 'tool';
  tool_call_id: string;
  name: string;
  result: unknown;
  success: boolean;
  duration_ms: number | null;
  created_at: string;
  compacted?: boolean;
}

export interface SystemRow {
  id: number;
  role: 'system';
  text?: string;
  created_at: string;
  compacted?: boolean;
}

export type MessageRow = UserRow | AssistantRow | ToolRow | SystemRow;

export interface SessionMessages {
  session_id: string;
  title: string;
  is_running: boolean;
  turn_id: string | null;
  todos: Todo[];
  usage: Usage;
  messages: MessageRow[];
}

// ─── Streaming events ──────────────────────────────────────────────

export interface RecallItem {
  id: number;
  content: string;
  category: string;
}

export interface SkillSuggestion {
  name: string;
  description: string;
  score?: number;
}

type Seq = { seq?: number };

export type StreamEvent =
  | ({ type: 'session'; session_id: string; turn_id: string; title: string; resumed?: boolean } & Seq)
  | ({ type: 'turn_start'; session_id: string; user_message_id: number | null; title: string } & Seq)
  | ({ type: 'memory_recall'; items: RecallItem[] } & Seq)
  | ({ type: 'skills_suggested'; skills: SkillSuggestion[] } & Seq)
  | ({ type: 'step_start'; step: number } & Seq)
  | ({ type: 'snapshot'; text?: string; reasoning?: string; step?: number } & Seq)
  | ({ type: 'text'; content: string } & Seq)
  | ({ type: 'reasoning'; content: string } & Seq)
  | ({ type: 'message'; message_id: number | null; role: string } & Seq)
  | ({ type: 'usage' } & Usage & Seq)
  | ({ type: 'tool_call'; id: string; tool: string; params: Record<string, unknown> } & Seq)
  | ({
      type: 'approval_requested';
      approval_id: string;
      tool_call_id: string;
      tool: string;
      params: Record<string, unknown>;
      reason: string;
      severity: string;
    } & Seq)
  | ({ type: 'approval'; approval_id: string; tool_call_id: string; approved: boolean } & Seq)
  | ({ type: 'tool_result'; id: string; tool: string; success: boolean; result: unknown; duration_ms: number } & Seq)
  | ({
      type: 'compaction';
      status: 'started' | 'done' | 'failed';
      messages?: number;
      summary_chars?: number;
      error?: string;
    } & Seq)
  | ({ type: 'title'; title: string } & Seq)
  | ({ type: 'error'; message: string } & Seq)
  | ({
      type: 'done';
      interrupted: boolean;
      error: string | null;
      final_response?: string;
      api_calls?: number;
      tool_calls?: number;
      duration_ms?: number;
      title?: string;
    } & Seq)
  | ({
      type: 'command';
      command: string;
      message: string;
      level: 'info' | 'error' | string;
      action: 'switch_session' | 'refresh_sessions' | null;
      session_id: string | null;
    } & Seq)
  | ({ type: 'idle'; session_id?: string } & Seq)
  | ({ type: 'stream_end' } & Seq);

export type StreamEventType = StreamEvent['type'];

export interface ChatRequest {
  message: string;
  session_id?: string | null;
  attachments?: string[];
  edit_message_id?: number;
  regenerate?: boolean;
}

export interface SlashCommand {
  name: string;
  description: string;
  usage: string;
}

// ─── Uploads ───────────────────────────────────────────────────────

export interface UploadResult {
  id: string;
  name: string;
  mime: string;
  size: number;
  kind: 'image' | 'text' | 'file';
  url: string;
}

// ─── Memory ────────────────────────────────────────────────────────

export interface MemoryBlock {
  name: string;
  label: string;
  description: string;
  limit: number;
  chars: number;
  content: string;
}

export interface MemoryOverview {
  blocks: MemoryBlock[];
  stats: MemoryStats;
}

export const MEMORY_CATEGORIES = ['fact', 'preference', 'person', 'project', 'event', 'procedure', 'other'] as const;
export type MemoryCategory = (typeof MEMORY_CATEGORIES)[number];

export interface MemoryEntry {
  id: number;
  content: string;
  category: string;
  importance: number;
  source_session: string | null;
  created_at: string;
  updated_at: string;
  last_accessed: string | null;
  access_count: number;
  relevance?: number;
}

// ─── Skills ────────────────────────────────────────────────────────

export interface Skill {
  name: string;
  description: string;
  tags: string[];
  source: 'builtin' | 'user';
  path: string;
  updated_at: string | null;
  editable: boolean;
  usage_count: number;
  last_used: string | null;
}

export interface SkillDetail extends Skill {
  content: string;
  raw: string;
  files: string[];
}

// ─── Persona / settings ────────────────────────────────────────────

export interface Personality {
  persona: string;
  instructions: string;
  default: string;
  is_default: boolean;
}

export interface SystemPromptPreview {
  prompt: string;
  chars: number;
  approx_tokens: number;
}

export interface ProviderInfo {
  id: string;
  name: string;
  description: string;
  needs_key: boolean;
  has_key: boolean;
  default_model: string;
  model: string;
  base_url: string;
  default_base_url: string;
  models: string[];
  env_key: string | null;
  env_model: string | null;
  env_base_url: string | null;
  local?: boolean;
}

export interface LiquidModelStatus {
  repo: string;
  file: string;
  quant: string;
  path: string;
  downloaded: boolean;
  size_bytes: number;
  runtime: string | null;
  runtime_available: boolean;
  runtime_error: string | null;
  context: number;
  choices: string[];
  state: 'idle' | 'downloading' | 'ready' | 'error' | string;
  progress: number;
  downloaded_bytes: number;
  total_bytes: number;
  error: string | null;
  started_at: number | null;
  finished_at: number | null;
}

export type ApprovalMode = 'auto' | 'always' | 'never';

export interface AgentSettings {
  allow_host_exec: boolean;
  max_iterations: number;
  temperature: number;
  context_window: number;
  memory_auto_extract: boolean;
  memory_auto_recall: boolean;
  skills_auto_create: boolean;
  auto_title: boolean;
  approval_mode: ApprovalMode | string;
  headless_approval: 'deny' | 'allow' | string;
}

export interface Settings {
  provider: string;
  model: string;
  context_window: number;
  providers: ProviderInfo[];
  liquid?: LiquidModelStatus;
  agent: AgentSettings;
  easy_diffusion_url: string;
  voice?: VoiceStatus;
  workspace: string;
  home: string;
  docker_available: boolean;
  message?: string;
}

export interface SettingsUpdate {
  provider?: string;
  model?: string;
  api_keys?: Record<string, string>;
  models?: Record<string, string>;
  base_urls?: Record<string, string>;
  agent?: Partial<AgentSettings>;
  easy_diffusion_url?: string;
  elevenlabs_api_key?: string;
  elevenlabs_model?: string;
}

export interface VoiceSettings {
  stability: number;
  similarity_boost: number;
  style: number;
  speed: number;
  use_speaker_boost: boolean;
  instructions?: string;
}

export interface VoiceProviderInfo {
  id: 'edge' | 'browser' | 'elevenlabs' | 'openai' | string;
  label: string;
  description: string;
  free: boolean;
  requires_key: boolean;
  available: boolean;
  error: string | null;
  models: string[];
  supports_preview: boolean;
  max_chars: number;
}

export interface VoiceInfo {
  voice_id: string;
  name: string;
  provider: string;
  gender?: string | null;
  age?: string | null;
  accent?: string | null;
  locale?: string | null;
  locale_name?: string | null;
  description?: string | null;
  tags: string[];
  preview_url?: string | null;
  public_owner_id?: string;
  current?: boolean;
}

export interface Voice {
  voice_id: string;
  name: string;
  provider: string;
  settings: VoiceSettings;
  reason?: string | null;
  chosen_at?: string | null;
  default?: boolean;
}

export interface VoiceStatus {
  configured: boolean;
  provider: string;
  provider_label: string;
  free: boolean;
  model: string;
  max_chars: number;
  voice: Voice | null;
  settings: VoiceSettings;
  providers: VoiceProviderInfo[];
  usage?: {
    characters_used: number | null;
    character_limit: number | null;
    tier: string | null;
    voice_slots_used: number | null;
    voice_slot_limit: number | null;
    resets_at_unix: number | null;
  } | null;
}

export interface PreparedSpeech {
  id: string;
  url: string;
  chars: number;
  truncated: boolean;
  voice: string;
}

export interface ConnectionTest {
  ok: boolean;
  latency_ms?: number;
  reply?: string;
  error?: string;
  provider: string;
  model: string;
}

export interface GmailStatus {
  authorized: boolean;
  has_client_secret: boolean;
}

// ─── Tools / approvals ─────────────────────────────────────────────

export interface ToolDetail {
  name: string;
  description: string;
  tags: string[];
}

export interface ToolsResponse {
  tools: unknown[];
  tool_names: string[];
  details: ToolDetail[];
}

export interface PendingApproval {
  approval_id: string;
  tool_name: string;
  params: Record<string, unknown>;
  reason: string;
  severity: string;
  created_at: number;
  age_seconds: number;
}

// ─── Kanban / gallery / jobs (unchanged endpoints) ─────────────────

export interface KanbanCard {
  id: string;
  title: string;
  description: string;
  column: string;
  priority: number;
  tags: string[];
  status: 'idle' | 'running' | 'succeeded' | 'failed' | 'blocked';
  result: Record<string, unknown>;
  execution_session_id: string;
  error: string;
  created_at: string;
  updated_at: string;
  executed_at: string;
  completed_at: string;
  auto_advance: boolean;
  max_iterations: number;
}

export interface GalleryImage {
  filename: string;
  size_bytes: number;
  created_at: string;
  url: string;
  prompt: string;
  negative_prompt: string;
  seed: number | null;
  sampler: string;
  steps: number;
  cfg_scale: number;
  width: number;
  height: number;
  task_id: string;
}

export interface ScheduledJob {
  id: string;
  name: string;
  description: string;
  prompt: string;
  cron_expr: string;
  timezone: string;
  enabled: boolean;
  last_run_at: string;
  next_run_at: string;
  run_count: number;
  created_at: string;
  updated_at: string;
}

export interface JobRun {
  id: string;
  job_id: string;
  status: 'running' | 'completed' | 'failed';
  prompt: string;
  response: string;
  error: string;
  tool_calls: Array<{ tool_name: string; success: boolean }>;
  started_at: string;
  completed_at: string;
  duration_ms: number;
}
