// Formatting helpers (dates, sizes, numbers).

/**
 * Parse a backend timestamp. SQLite timestamps look like "2026-10-13 09:59:18.123"
 * and are UTC without a zone marker; ISO strings with an offset are respected.
 */
export function parseUtc(ts: string | null | undefined): Date | null {
  if (!ts) return null;
  let s = String(ts).trim();
  if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(s)) {
    s = s.replace(' ', 'T');
    if (!/(Z|[+-]\d{2}:?\d{2})$/i.test(s)) s += 'Z';
  }
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

/** Parse a timestamp that is local time when no zone is given (e.g. Python isoformat()). */
export function parseLocal(ts: string | null | undefined): Date | null {
  if (!ts) return null;
  const d = new Date(String(ts).replace(' ', 'T'));
  return isNaN(d.getTime()) ? null : d;
}

export function timeOfDay(d: Date | null): string {
  if (!d) return '';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function shortDateTime(d: Date | null): string {
  if (!d) return '—';
  const now = new Date();
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    year: sameYear ? undefined : 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function relativeTime(d: Date | null): string {
  if (!d) return '';
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 45) return 'just now';
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  if (diff < 86400 * 7) return `${Math.round(diff / 86400)}d ago`;
  return d.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
}

export type DateGroup = 'Today' | 'Yesterday' | 'Previous 7 days' | 'Previous 30 days' | 'Older';
export const DATE_GROUPS: DateGroup[] = ['Today', 'Yesterday', 'Previous 7 days', 'Previous 30 days', 'Older'];

export function dateGroup(d: Date | null): DateGroup {
  if (!d) return 'Older';
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const day = 86400000;
  const t = d.getTime();
  if (t >= startOfToday.getTime()) return 'Today';
  if (t >= startOfToday.getTime() - day) return 'Yesterday';
  if (t >= startOfToday.getTime() - 7 * day) return 'Previous 7 days';
  if (t >= startOfToday.getTime() - 30 * day) return 'Previous 30 days';
  return 'Older';
}

export function formatBytes(bytes: number): string {
  if (!bytes && bytes !== 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '';
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60000);
  const s = Math.round((ms % 60000) / 1000);
  return `${m}m ${s}s`;
}

export function compactNumber(n: number | null | undefined): string {
  if (!n) return '0';
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}K`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

export function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}

/** Trigger a client-side download of text content. */
export function downloadText(filename: string, text: string, mime = 'text/markdown') {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function safeFilename(name: string): string {
  return (name || 'chat').replace(/[\\/:*?"<>|]+/g, '_').replace(/\s+/g, ' ').trim().slice(0, 80) || 'chat';
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }
}
