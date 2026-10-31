'use client';

import { useCallback, useEffect, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { Bot, Cpu, Plug, Settings as SettingsIcon, UserRound, Volume2 } from 'lucide-react';
import * as api from '@/lib/api';
import { cx } from '@/lib/format';
import type { Settings } from '@/lib/types';
import { CenteredSpinner, EmptyState, PageHeader } from '../ui';
import AgentSection from './AgentSection';
import IntegrationsSection from './IntegrationsSection';
import ModelSection from './ModelSection';
import PersonaSection from './PersonaSection';
import VoiceSection from './VoiceSection';

type Tab = 'model' | 'agent' | 'voice' | 'persona' | 'integrations';
const TABS: { id: Tab; label: string; icon: typeof Cpu }[] = [
  { id: 'model', label: 'Model', icon: Cpu },
  { id: 'agent', label: 'Agent', icon: Bot },
  { id: 'voice', label: 'Voice', icon: Volume2 },
  { id: 'persona', label: 'Persona', icon: UserRound },
  { id: 'integrations', label: 'Integrations', icon: Plug },
];

export default function SettingsView({ onSaved }: { onSaved?: () => void }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const params = useSearchParams();
  const router = useRouter();
  const initialTab = params.get('tab') as Tab | null;
  const [tab, setTabState] = useState<Tab>(
    initialTab && TABS.some((t) => t.id === initialTab) ? initialTab : 'model',
  );

  // Keep the tab in the URL so the page can be linked/reloaded (and Voice deep-linked).
  const setTab = (next: Tab) => {
    setTabState(next);
    const sp = new URLSearchParams(params.toString());
    sp.set('view', 'settings');
    sp.set('tab', next);
    router.replace(`/?${sp.toString()}`, { scroll: false });
  };

  const load = useCallback(() => {
    api
      .getSettings()
      .then((s) => {
        setSettings(s);
        setError(null);
      })
      .catch((e) => setError(api.errorMessage(e)));
  }, []);

  useEffect(load, [load]);

  const saved = (s: Settings) => {
    setSettings(s);
    onSaved?.();
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Settings" icon={<SettingsIcon className="h-4 w-4" />} subtitle="Model provider, agent behaviour, persona and integrations" />
      <div className="flex-none border-b border-line px-3 md:px-6">
        <div className="flex gap-1 overflow-x-auto" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={tab === t.id}
              onClick={() => setTab(t.id)}
              className={cx(
                '-mb-px flex items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2.5 text-[13px] font-medium transition-colors',
                tab === t.id ? 'border-primary text-fg' : 'border-transparent text-muted hover:text-fg-2',
              )}
            >
              <t.icon className="h-4 w-4" />
              {t.label}
            </button>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl px-5 py-6 md:px-8">
          {error && !settings && (
            <EmptyState icon={<SettingsIcon className="h-5 w-5" />} title="Could not load settings">
              {error}
              <div className="mt-3">
                <button className="btn-secondary" onClick={load}>
                  Retry
                </button>
              </div>
            </EmptyState>
          )}
          {!settings && !error && (
            <div className="h-64">
              <CenteredSpinner />
            </div>
          )}
          {settings && tab === 'model' && <ModelSection settings={settings} onSaved={saved} />}
          {settings && tab === 'agent' && <AgentSection settings={settings} onSaved={saved} />}
          {settings && tab === 'voice' && <VoiceSection settings={settings} onSaved={saved} />}
          {tab === 'persona' && <PersonaSection />}
          {settings && tab === 'integrations' && <IntegrationsSection settings={settings} onSaved={saved} />}
        </div>
      </div>
    </div>
  );
}
