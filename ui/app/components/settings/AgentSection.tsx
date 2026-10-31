'use client';

import { useEffect, useState, type ReactNode } from 'react';
import { Bot, Save, TriangleAlert } from 'lucide-react';
import * as api from '@/lib/api';
import { cx } from '@/lib/format';
import type { AgentSettings, Settings } from '@/lib/types';
import { useToast } from '../Toaster';
import { Section, Spinner, Toggle } from '../ui';

function Row({ title, description, children, warning }: { title: string; description: ReactNode; children: ReactNode; warning?: ReactNode }) {
  return (
    <div className="flex flex-col gap-2 py-3.5 first:pt-0 last:pb-0 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
      <div className="min-w-0">
        <p className="text-[13px] font-medium text-fg">{title}</p>
        <p className="mt-0.5 text-xs leading-relaxed text-muted">{description}</p>
        {warning}
      </div>
      <div className="flex-none sm:pt-0.5">{children}</div>
    </div>
  );
}

const APPROVAL_HELP: Record<string, string> = {
  auto: 'Ask before destructive actions (deleting files, force pushes, DROP/DELETE SQL, risky shell commands).',
  always: 'Ask before every tool call.',
  never: 'Never ask. Alita runs every tool without confirmation.',
};

export default function AgentSection({ settings, onSaved }: { settings: Settings; onSaved: (s: Settings) => void }) {
  const toast = useToast();
  const [draft, setDraft] = useState<AgentSettings>(settings.agent);
  const [saving, setSaving] = useState(false);
  useEffect(() => setDraft(settings.agent), [settings.agent]);

  const changed = (Object.keys(draft) as (keyof AgentSettings)[]).filter((k) => draft[k] !== settings.agent[k]);
  const set = <K extends keyof AgentSettings>(k: K, v: AgentSettings[K]) => setDraft((d) => ({ ...d, [k]: v }));

  const save = async () => {
    const patch: Partial<AgentSettings> = {};
    changed.forEach((k) => {
      (patch as Record<string, unknown>)[k] = draft[k];
    });
    setSaving(true);
    try {
      const res = await api.updateSettings({ agent: patch });
      onSaved(res);
      toast.success('Agent settings saved');
    } catch (e) {
      toast.error(api.errorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  const num = (k: 'max_iterations' | 'temperature' | 'context_window', v: string) => {
    const n = k === 'temperature' ? parseFloat(v) : parseInt(v, 10);
    set(k, (isNaN(n) ? 0 : n) as AgentSettings[typeof k]);
  };

  return (
    <Section
      title="Agent"
      icon={<Bot className="h-4 w-4" />}
      description="How Alita behaves while working. Changes apply immediately."
      actions={
        <button className="btn-primary" onClick={save} disabled={saving || changed.length === 0}>
          {saving ? <Spinner className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />} Save
          {changed.length > 0 && <span className="rounded bg-white/20 px-1 text-[10px]">{changed.length}</span>}
        </button>
      }
    >
      <div className="divide-y divide-line">
        <Row
          title="Allow host command execution"
          description={
            <>
              Lets the terminal and code tools run directly on this machine.{' '}
              {settings.docker_available ? 'Docker is available, so sandboxed tools still work when this is off.' : 'Docker was not detected, so without this most code tools are unavailable.'}
            </>
          }
          warning={
            draft.allow_host_exec ? (
              <p className="mt-2 flex items-start gap-1.5 rounded-lg border border-warn/30 bg-warn/[0.07] px-2.5 py-1.5 text-[11.5px] leading-relaxed text-warn">
                <TriangleAlert className="mt-px h-3.5 w-3.5 flex-none" />
                Alita will run shell commands on this computer with your user permissions. Keep approvals on.
              </p>
            ) : null
          }
        >
          <Toggle checked={draft.allow_host_exec} onChange={(v) => set('allow_host_exec', v)} label="Allow host command execution" />
        </Row>
        <Row title="Tool approvals" description={APPROVAL_HELP[draft.approval_mode] || 'Approval policy for tool calls.'}>
          <select value={draft.approval_mode} onChange={(e) => set('approval_mode', e.target.value)} className="input w-auto py-1.5 text-[13px]" aria-label="Tool approvals">
            <option value="auto">Destructive only</option>
            <option value="always">Every tool call</option>
            <option value="never">Never ask</option>
          </select>
        </Row>
        <Row title="Background runs" description="Scheduled jobs, board cards and sub-agents can’t ask you. Choose what happens when they need approval.">
          <select value={draft.headless_approval} onChange={(e) => set('headless_approval', e.target.value)} className="input w-auto py-1.5 text-[13px]" aria-label="Background approvals">
            <option value="deny">Deny</option>
            <option value="allow">Allow</option>
          </select>
        </Row>
        <Row title="Max steps per turn" description="Upper bound on model calls in one reply before Alita must wrap up.">
          <input type="number" min={1} max={200} value={draft.max_iterations} onChange={(e) => num('max_iterations', e.target.value)} className="input w-24 py-1.5 text-right font-mono" aria-label="Max steps per turn" />
        </Row>
        <Row title="Temperature" description="Lower is more focused and deterministic; higher is more creative.">
          <input type="number" min={0} max={2} step={0.1} value={draft.temperature} onChange={(e) => num('temperature', e.target.value)} className="input w-24 py-1.5 text-right font-mono" aria-label="Temperature" />
        </Row>
        <Row title="Context window" description={<>Tokens available to the model. 0 uses the provider default (currently ~{settings.context_window.toLocaleString()}).</>}>
          <input type="number" min={0} step={1024} value={draft.context_window} onChange={(e) => num('context_window', e.target.value)} className="input w-32 py-1.5 text-right font-mono" aria-label="Context window" />
        </Row>
        <Row title="Recall memories automatically" description="Search long-term memory for each message and give relevant items to Alita.">
          <Toggle checked={draft.memory_auto_recall} onChange={(v) => set('memory_auto_recall', v)} label="Recall memories automatically" />
        </Row>
        <Row title="Learn memories automatically" description="After each reply, extract durable facts and preferences (one extra small model call).">
          <Toggle checked={draft.memory_auto_extract} onChange={(v) => set('memory_auto_extract', v)} label="Learn memories automatically" />
        </Row>
        <Row title="Create skills automatically" description="After complex multi-step tasks, save the procedure as a reusable skill.">
          <Toggle checked={draft.skills_auto_create} onChange={(v) => set('skills_auto_create', v)} label="Create skills automatically" />
        </Row>
        <Row title="Generate chat titles" description="Name new chats with a short model-written title.">
          <Toggle checked={draft.auto_title} onChange={(v) => set('auto_title', v)} label="Generate chat titles" />
        </Row>
      </div>
      <p className={cx('mt-4 text-[11px] text-muted-2')}>
        Workspace: <span className="font-mono">{settings.workspace}</span> · Data: <span className="font-mono">{settings.home}</span>
      </p>
    </Section>
  );
}
