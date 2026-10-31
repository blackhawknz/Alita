// Human-friendly presentation of tool calls (icon, verb, parameter summary).

import {
  AudioLines,
  BookOpen,
  Brain,
  Code2,
  Database,
  FileText,
  GitBranch,
  Globe,
  Hammer,
  History,
  Kanban,
  ListChecks,
  Mail,
  Network,
  NotebookPen,
  Palette,
  Plug,
  Split,
  Terminal,
  Wrench,
  type LucideIcon,
} from 'lucide-react';

export interface ToolPresentation {
  icon: LucideIcon;
  color: string;
  /** Verb phrase, already in the right tense */
  label: string;
  /** Short parameter summary */
  detail: string;
}

const str = (v: unknown): string => (typeof v === 'string' ? v : v === undefined || v === null ? '' : JSON.stringify(v));
const firstLine = (s: string) => s.split('\n').find((l) => l.trim())?.trim() ?? '';
const clip = (s: string, n = 90) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
const cap = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
const humanize = (s: string) => cap(s.replace(/[_-]+/g, ' ').trim());

function pick(running: boolean, active: string, past: string) {
  return running ? active : past;
}

export function describeTool(name: string, params: Record<string, unknown>, running: boolean): ToolPresentation {
  const p = params || {};
  const action = str(p.action);
  switch (name) {
    case 'terminal':
      return { icon: Terminal, color: 'var(--teal)', label: pick(running, 'Running command', 'Ran command'), detail: clip(firstLine(str(p.command))) };
    case 'file_ops': {
      const verbs: Record<string, [string, string]> = {
        read: ['Reading', 'Read'],
        write: ['Writing', 'Wrote'],
        patch: ['Editing', 'Edited'],
        append: ['Appending to', 'Appended to'],
        list: ['Listing', 'Listed'],
        search: ['Searching files in', 'Searched files in'],
        delete: ['Deleting', 'Deleted'],
        mkdir: ['Creating folder', 'Created folder'],
        move: ['Moving', 'Moved'],
        copy: ['Copying', 'Copied'],
        list_checkpoints: ['Listing checkpoints', 'Listed checkpoints'],
        rollback: ['Rolling back', 'Rolled back'],
      };
      const [a, b] = verbs[action] || [`${humanize(action || 'file op')}`, `${humanize(action || 'file op')}`];
      const target = str(p.path) || (action === 'list' || action === 'search' ? 'workspace' : '');
      const q = action === 'search' && p.query ? ` for "${str(p.query)}"` : '';
      return { icon: FileText, color: 'var(--accent)', label: pick(running, a, b), detail: clip(`${target}${q}`) };
    }
    case 'browser':
      if (action === 'search') {
        return { icon: Globe, color: 'var(--warning)', label: pick(running, 'Searching the web', 'Searched the web'), detail: clip(str(p.query)) };
      }
      return {
        icon: Globe,
        color: 'var(--warning)',
        label: pick(running, action === 'summarize' ? 'Summarising page' : 'Reading page', action === 'summarize' ? 'Summarised page' : 'Read page'),
        detail: clip(str(p.url)),
      };
    case 'http_request':
      return {
        icon: Network,
        color: 'var(--cyan)',
        label: pick(running, 'Sending request', 'Sent request'),
        detail: clip(`${(str(p.method) || 'GET').toUpperCase()} ${str(p.url)}`),
      };
    case 'execute_code': {
      const lang = str(p.language) || 'python';
      return { icon: Code2, color: 'var(--cyan)', label: pick(running, `Running ${lang}`, `Ran ${lang}`), detail: clip(firstLine(str(p.code))) };
    }
    case 'memory': {
      const verbs: Record<string, [string, string, unknown]> = {
        save: ['Saving memory', 'Saved memory', p.content],
        search: ['Searching memory', 'Searched memory', p.query],
        update: ['Updating memory', 'Updated memory', p.content || (p.id ? `#${p.id}` : '')],
        delete: ['Deleting memory', 'Deleted memory', p.id ? `#${p.id}` : ''],
        list: ['Listing memories', 'Listed memories', p.category],
      };
      const [a, b, d] = verbs[action] || ['Using memory', 'Used memory', action];
      return { icon: Brain, color: 'var(--purple)', label: pick(running, a, b), detail: clip(str(d)) };
    }
    case 'core_memory':
      return {
        icon: NotebookPen,
        color: 'var(--purple)',
        label: action === 'view' ? pick(running, 'Viewing core memory', 'Viewed core memory') : pick(running, 'Updating core memory', 'Updated core memory'),
        detail: clip([str(p.block), action !== 'view' ? action : ''].filter(Boolean).join(' · ')),
      };
    case 'skill_view':
      return { icon: BookOpen, color: 'var(--primary)', label: pick(running, 'Reading skill', 'Read skill'), detail: clip([str(p.name), str(p.file)].filter(Boolean).join(' / ')) };
    case 'skill_manage':
      return { icon: Hammer, color: 'var(--primary)', label: `${humanize(action || 'manage')} skill`, detail: clip(str(p.name)) };
    case 'delegate_task':
      return { icon: Split, color: 'var(--purple)', label: pick(running, 'Delegating task', 'Delegated task'), detail: clip(firstLine(str(p.task))) };
    case 'todo': {
      const n = Array.isArray(p.todos) ? p.todos.length : 0;
      return {
        icon: ListChecks,
        color: 'var(--teal)',
        label: n ? pick(running, 'Updating plan', 'Updated plan') : pick(running, 'Reading plan', 'Read plan'),
        detail: n ? `${n} item${n === 1 ? '' : 's'}` : '',
      };
    }
    case 'easy_diffusion':
      if (action === 'models') return { icon: Palette, color: 'var(--purple)', label: pick(running, 'Listing image models', 'Listed image models'), detail: '' };
      if (action === 'status') return { icon: Palette, color: 'var(--purple)', label: pick(running, 'Checking image job', 'Checked image job'), detail: '' };
      return { icon: Palette, color: 'var(--purple)', label: pick(running, 'Generating image', 'Generated image'), detail: clip(str(p.prompt)) };
    case 'git':
      return { icon: GitBranch, color: 'var(--warning)', label: `git ${action || ''}`.trim(), detail: clip(Array.isArray(p.args) ? p.args.join(' ') : str(p.args)) };
    case 'database':
      return {
        icon: Database,
        color: 'var(--accent)',
        label: action === 'schema' ? pick(running, 'Reading schema', 'Read schema') : pick(running, 'Querying database', 'Queried database'),
        detail: clip(firstLine(str(p.sql)) || str(p.db_path)),
      };
    case 'gmail':
      return {
        icon: Mail,
        color: 'var(--error)',
        label: action === 'read' ? pick(running, 'Reading email', 'Read email') : pick(running, 'Checking Gmail', 'Checked Gmail'),
        detail: clip(str(p.query) || str(p.message_id) || action),
      };
    case 'kanban':
      return { icon: Kanban, color: 'var(--teal)', label: `Board: ${humanize(action || 'action').toLowerCase()}`, detail: clip(str(p.title) || str(p.card_id)) };
    case 'session_search':
      return { icon: History, color: 'var(--warning)', label: pick(running, 'Searching past chats', 'Searched past chats'), detail: clip(str(p.query)) };
    case 'voice': {
      const filters = [p.search, p.gender, p.age, p.accent].map(str).filter(Boolean).join(' · ');
      if (action === 'list') {
        const where = str(p.source) === 'library' ? 'voice library' : 'voices';
        return { icon: AudioLines, color: 'var(--purple)', label: pick(running, `Browsing ${where}`, `Browsed ${where}`), detail: clip(filters) };
      }
      if (action === 'preview') {
        return { icon: AudioLines, color: 'var(--purple)', label: pick(running, 'Preparing voice preview', 'Voice preview'), detail: clip(str(p.text) || str(p.voice_id)) };
      }
      if (action === 'set') {
        const tuning = !p.voice_id;
        return {
          icon: AudioLines,
          color: 'var(--purple)',
          label: tuning ? pick(running, 'Tuning her voice', 'Tuned her voice') : pick(running, 'Choosing her voice', 'Chose her voice'),
          detail: clip(str(p.reason)),
        };
      }
      return { icon: AudioLines, color: 'var(--purple)', label: pick(running, 'Checking her voice', 'Checked her voice'), detail: '' };
    }
  }
  if (name.startsWith('mcp__')) {
    const [, server = '', tool = ''] = name.split('__');
    return { icon: Plug, color: 'var(--teal)', label: `${server} › ${tool.replace(/__/g, ' › ')}`, detail: clip(summarizeParams(p)) };
  }
  return { icon: Wrench, color: 'var(--muted)', label: humanize(name || 'tool'), detail: clip(summarizeParams(p)) };
}

export function summarizeParams(params: Record<string, unknown>): string {
  return Object.values(params || {})
    .filter((v) => v !== undefined && v !== null && v !== '')
    .map((v) => firstLine(str(v)))
    .join(' · ');
}
