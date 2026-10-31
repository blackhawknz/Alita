# Alita — Self-Hosted Autonomous AI Agent

Alita is a personal AI agent that runs on your own machine. She chats, researches the web, writes and runs code, generates images, reads your Gmail, runs scheduled jobs, works a Kanban board with sub-agents, **remembers you across conversations**, and **writes her own skills** as she solves recurring tasks.

- **Any model**: Ollama, OpenAI, Anthropic, DeepSeek, Gemini, OpenRouter, xAI, Qwen, Z.AI, MiniMax, Liquid AI, or any OpenAI-compatible server (LM Studio, vLLM, llama.cpp). You can switch providers live from Settings.
- **Works offline**: Alita ships a managed Liquid AI local provider. Pick **Liquid AI (offline)** and the LFM GGUF is downloaded once into `~/.alita/models/liquid`; after that it runs with no internet and no API key. Online providers can also fall back to it automatically.
- **Chat-first UI**: session sidebar with search, pinning and rename; streaming replies with live tool cards; Stop; inline approvals; edit and regenerate; file and image attachments; slash commands; export.
- **Hands-free voice**: talk to Alita naturally without push-to-talk. Silence ends your turn, her spoken reply plays, then the microphone re-opens. Push-to-talk and click-to-talk are available too.

---

## Architecture

```
                 ┌──────────────── Next.js UI (:3001) ────────────────┐
                 │ chat · memory · skills · board · jobs · gallery    │
                 └───────────────┬────────────────────────────────────┘
                                 │ /api/* (SSE streaming, resumable)
┌────────────────────────────────▼──────────────────────────────────────┐
│ FastAPI (:8080)                                                       │
│  AlitaAgent (shared runtime)          Session (one per conversation)  │
│  ├─ LLM provider (hot-swappable)      ├─ full message history         │
│  ├─ tools · MCP · approvals           ├─ todo plan · interrupt flag   │
│  ├─ memory manager · skills           └─ TurnRun (event buffer)       │
│  └─ scheduler · kanban executor                                       │
│                                                                       │
│  Turn loop: persist user msg → recall memories & skills → build       │
│  prompt → compact if near window → stream model → run tools           │
│  (approval gates, parallel when safe) → persist each step → repeat    │
│  → done → background: extract memories, distil skill, title chat      │
└───────────────────────────────────────────────────────────────────────┘
```

Each conversation is an isolated session: chat, scheduled-job run, sub-agent task or kanban card. Any number of sessions can run at once. Every message, tool call and tool result is persisted the moment it happens, so history survives restarts and replays exactly. If you switch away from a busy chat it keeps working, and switching back re-attaches to the live stream.

### Memory

| Layer | What it holds | How it's used |
|---|---|---|
| **Persona** (`~/.alita/data/SOUL.md`) + **custom instructions** | Who Alita is, how you want her to behave | Top of every system prompt (editable in Settings → Persona) |
| **Core memory** (`~/.alita/memory/user.md`, `notes.md`) | Your profile and standing preferences; Alita's durable working notes | Always in context. Alita edits these herself (`core_memory` tool); character-limited so they stay dense |
| **Long-term memory** (SQLite + FTS5 + local embeddings) | Unlimited atomic facts: preferences, people, projects, events, procedures | **Auto-recall**: relevant memories are injected into each message by hybrid keyword + semantic search, ranked by importance and recency. **Auto-extraction**: after each exchange a background pass adds new facts, updates outdated ones and deletes contradicted ones, de-duplicating as it goes |
| **Transcripts** | Every conversation | Full-text `session_search` tool and sidebar search |

Long conversations are **compacted**. Near the context limit, older turns are summarised into a structured digest that replaces them in context, while the full transcript stays in the database and in the UI. Bulky outputs from older tool calls are trimmed automatically.

Manage everything on the **Memory** page, or with `/remember`, `/recall` and `/forget` in chat.

### Skills

Skills follow the [Agent Skills](https://docs.anthropic.com/en/docs/agents-and-tools/agent-skills) format: a directory with a `SKILL.md` (YAML front-matter `name` and `description`, then markdown instructions), plus optional scripts or templates.

- **Progressive disclosure**: only names and descriptions sit in the prompt. Alita loads a skill with `skill_view` when it's relevant, and suggests matching skills each turn.
- **Self-authoring**: Alita creates and patches her own skills with `skill_manage`. After a complex successful task, a background reflection decides whether to distil it into a new skill or improve an existing one.
- Built-in skills live in `backend/skills/`; your skills live in `~/.alita/skills/`. A user skill overrides a built-in one with the same name. Browse and edit them on the **Skills** page.

### Voice

Alita can speak her replies, in a voice she picks herself (or you pick for her).

| Provider | Cost | Notes |
|---|---|---|
| **Edge neural voices** *(default)* | **Free**, no key | Microsoft's natural voices, 300+ across languages. Uses the `edge-tts` package (installed from `requirements.txt`). |
| **Browser voices** | **Free**, offline | The device's installed voices via the Web Speech API. Quality varies by OS. |
| **ElevenLabs** | Paid (credits) | Best quality and voice cloning; can import public library voices. |
| **OpenAI TTS** | Cheap | Uses your OpenAI key; tone can be described in words (`gpt-4o-mini-tts`). |

- **Choose** in **Settings → Voice**: switch provider, search and preview voices, pick one, and tune speed (plus stability / similarity / style for ElevenLabs, or tone instructions for OpenAI).
- **Alita can choose herself**: ask "pick a warm British voice" or "slow down a bit". The `voice` tool lets her list, preview, set and tune voices across providers.
- **Listen**: use the speaker button under any reply, or turn on **Read replies aloud** (chat header or Voice settings) to have new replies played automatically.
- **Efficient**: audio is cached in `~/.alita/voice_cache/`, so replaying a message is free; long replies are capped per provider (2,500 chars for ElevenLabs, 5,000 for Edge/OpenAI).
- **Hands-free conversation**: click the microphone in the chat header (or enable it in **Settings → Voice**) and just talk. A configurable pause ends your turn; Alita answers aloud and starts listening again after she finishes. The microphone pauses while she speaks so she never hears herself. Choose the spoken language and end-of-turn pause in Settings; use the composer microphone for click-to-talk or hold-to-talk (push-to-talk).
- Your choice is stored in `~/.alita/voice.json`. With the free Edge provider there is nothing to configure; add `ELEVENLABS_API_KEY` only if you want ElevenLabs.

### Offline models

**Settings → Model → Liquid AI (offline)** runs a Liquid Foundation Model on this machine. The first use downloads the GGUF from Hugging Face (about 0.8 GB for the default `LFM2.5-1.2B-Instruct` Q4_K_M); afterwards it is cached in `~/.alita/models/liquid` and works with no internet and no API key. With `ALITA_LIQUID_FALLBACK=true` (default), a turn whose online provider has no key, no connection, or is rate-limited automatically continues on the local model.

---

## Quick start

Prerequisites: Python 3.11+, Node.js 20+, and a model: an API key, [Ollama](https://ollama.com) running locally, or the built-in Liquid AI offline model (downloaded once on first use).

**Windows:** `start.bat` · **macOS/Linux:** `./start.sh`

Or manually:

```bash
# Backend
cd backend
python -m venv venv
venv/Scripts/activate        # Windows; use `source venv/bin/activate` elsewhere
pip install -r requirements.txt
cp .env.example .env         # optional; you can configure everything in the UI
python run.py                # http://localhost:8080  (docs at /docs)

# UI (second terminal)
cd ui
npm install
npm run dev -- -p 3001       # http://localhost:3001
```

Open the UI, go to **Settings → Model**, pick a provider, paste a key and click **Test connection**. Settings apply immediately, with no restart.

**Docker:** `docker compose up -d --build`. The UI is on http://localhost:3001; configuration comes from `backend/.env`.

### Letting Alita run commands

The `terminal` and `execute_code` tools are **off on the host by default**. Enable them in **Settings → Agent → Allow host command execution**, or run the backend in Docker. Destructive operations still pause for your approval in auto mode: deleting or overwriting files, `rm`/`Remove-Item`, `git push --force`/`reset --hard`, SQL writes, and similar. Background runs (jobs, sub-agents, kanban) can't ask, so they deny such calls unless `ALITA_HEADLESS_APPROVAL=allow`.

---

## Configuration

Everything is in `backend/.env` (see [`backend/.env.example`](backend/.env.example) for every option), and the common settings are editable live in the UI. Key options:

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | Active provider |
| `<PROVIDER>_API_KEY` / `_MODEL` / `_BASE_URL` | — | Per-provider credentials, model and endpoint |
| `ALITA_HOME` | `~/.alita` | Data directory (database, memory, skills, uploads) |
| `ALITA_WORKSPACE` | `backend/workspace` | Where file and terminal tools operate |
| `ALITA_ALLOW_HOST_EXEC` | `false` | Allow shell and code execution on the host |
| `ALITA_MEMORY_AUTO_RECALL` / `_AUTO_EXTRACT` | `true` | Memory automation |
| `ALITA_SKILLS_AUTO_CREATE` | `true` | Distil complex tasks into skills |
| `ALITA_MAX_ITERATIONS` | `50` | Tool steps per turn |
| `ALITA_BACKGROUND_TIMEOUT` | `120` | Seconds before a background task is cancelled |
| `ALITA_APPROVAL_MODE` | `auto` | `auto` / `always` / `never` |
| `ALITA_API_TOKEN` | — | Require a bearer token (needed to bind non-loopback) |
| `ALITA_TIMEZONE` | host local | Default IANA timezone for scheduled jobs (per-job override available) |
| `ELEVENLABS_API_KEY` | — | Enables the ElevenLabs voice provider |
| `ELEVENLABS_MODEL` | `eleven_multilingual_v2` | ElevenLabs speech model |
| `OPENAI_TTS_MODEL` | `gpt-4o-mini-tts` | OpenAI speech model (uses your OpenAI key) |
| `LIQUID_API_KEY` / `LIQUID_MODEL` | — | Hosted Liquid AI (OpenAI-compatible) |
| `LIQUID_LOCAL_MODEL` | `LiquidAI/LFM2.5-1.2B-Instruct-GGUF` | Offline Liquid GGUF repo |
| `LIQUID_LOCAL_CONTEXT` / `_THREADS` / `_GPU_LAYERS` | `8192` / CPU | Offline model runtime tuning |
| `LIQUID_LOCAL_PATH` | — | Use an existing GGUF instead of the managed cache |
| `ALITA_LIQUID_FALLBACK` | `true` | Fall back to offline Liquid when an online provider is unavailable |

MCP servers: put a config in `~/.alita/mcp_servers.json` (example in `backend/alita/data/mcp_servers.json.example`). Their tools appear as `mcp__<server>__<tool>`.

---

## Slash commands

`/help` · `/new` · `/title <t>` · `/model [provider:]model` · `/compact` · `/usage` · `/memory` · `/remember <fact>` · `/recall <query>` · `/forget <id>` · `/skills` · `/pending` · `/approve [id]` · `/deny [id]` · `/mcp`

## API overview

Interactive docs are at `http://localhost:8080/docs`. Main groups:

- **Chat**: `POST /chat/stream` (SSE; creates a session if none is given; supports `attachments`, `edit_message_id` and `regenerate`) · `GET /sessions/{id}/stream?after=N` (re-attach) · `POST /sessions/{id}/interrupt` · `POST /chat` (non-streaming)
- **Sessions**: `GET/POST /sessions` · `GET/PATCH/DELETE /sessions/{id}` · `GET /sessions/{id}/messages` · `GET /sessions/search?q=` · `POST /sessions/{id}/compact` · `GET /sessions/{id}/export`
- **Memory**: `GET /memory` · `PUT /memory/blocks/{name}` · `GET/POST /memory/entries` · `PATCH/DELETE /memory/entries/{id}`
- **Skills**: `GET/POST /skills` · `GET/PUT/DELETE /skills/{name}`
- **Settings**: `GET/PUT /settings` · `POST /settings/test` · `GET/PUT /personality`
- **Voice**: `GET /voice/status` · `GET /voice/providers` · `GET /voice/voices?provider=` · `PUT /voice` · `POST /voice/preview` · `POST /voice/speech` + `GET /voice/speech/{id}.mp3`
- Also: `/uploads`, `/approvals/pending` + `/approve`, `/kanban/*`, `/jobs/*`, `/images/*`, `/gmail/*`, `/tools`, `/commands`, `/health`, `/stats`

## Project structure

```
backend/
  alita/
    core/       agent loop, sessions, prompt builder, context/compaction, approvals, slash commands, config
    memory/     sqlite_store (sessions, messages, FTS), core_memory, longterm, manager, vector_store
    skills/     manager (discovery, editing, relevance, reflection)
    llm/        provider adapters (OpenAI-compatible, Anthropic, Ollama), credential pool
    voice/      text-to-speech providers (Edge, browser, ElevenLabs, OpenAI), voice choice, caching, tool
    tools/      terminal/sandbox, file_ops, browser, http, code, git, database, images, gmail, MCP, sub-agents, kanban
    scheduler/  cron jobs        kanban/  board store + executor
    api/        FastAPI server, auth
  skills/       built-in skills (SKILL.md)
  tests/        pytest suite (run: venv/Scripts/python -m pytest)
ui/
  app/components/   AppShell, sidebar/, chat/, settings/, Memory/Skills/Kanban/Jobs/Gallery/System views
  lib/              api client, SSE stream + reconnect, turn reducer, types
```

## Security notes

- The API binds to `127.0.0.1` by default. Binding another interface requires `ALITA_API_TOKEN`; the UI then needs `NEXT_PUBLIC_ALITA_TOKEN`.
- Web tools refuse private and loopback addresses unless `ALITA_ALLOW_PRIVATE_URLS=true`.
- File tools are confined to the workspace and checkpoint every change, so edits can be rolled back.
- API keys are never returned by the API. Never commit `backend/.env` or `client_secret*.json`.

## License

MIT
