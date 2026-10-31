'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronRight, CircleCheck, CircleX, Cpu, Download, HardDrive, KeyRound, Plug, Save } from 'lucide-react';
import * as api from '@/lib/api';
import { cx, formatBytes } from '@/lib/format';
import type { ConnectionTest, LiquidModelStatus, ProviderInfo, Settings } from '@/lib/types';
import { useToast } from '../Toaster';
import { Section, Spinner } from '../ui';

const LOCAL_URL_PROVIDERS = new Set(['ollama', 'custom']);

export default function ModelSection({ settings, onSaved }: { settings: Settings; onSaved: (s: Settings) => void }) {
  const toast = useToast();
  const [selected, setSelected] = useState(settings.provider);
  const provider = useMemo(() => settings.providers.find((p) => p.id === selected) || settings.providers[0], [settings, selected]);
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [advanced, setAdvanced] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<ConnectionTest | null>(null);
  const [liquid, setLiquid] = useState<LiquidModelStatus | null>(settings.liquid || null);
  const [preparing, setPreparing] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!provider) return;
    setModel(provider.model || provider.default_model || '');
    setApiKey('');
    setBaseUrl(provider.base_url || '');
    setAdvanced(false);
  }, [provider]);

  useEffect(() => {
    if (settings.liquid) setLiquid(settings.liquid);
  }, [settings.liquid]);

  const busy = liquid?.state === 'downloading';

  useEffect(() => {
    if (provider?.id !== 'liquid_local') return;
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(() => {
      api.getLiquidStatus().then(setLiquid).catch(() => undefined);
    }, busy ? 1200 : 5000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [provider?.id, busy]);

  if (!provider) return null;

  const isActive = provider.id === settings.provider;
  const showUrl = LOCAL_URL_PROVIDERS.has(provider.id);
  const isLocal = !!provider.local;
  const keyField = provider.needs_key || provider.id === 'custom';
  const missingKey = provider.needs_key && !provider.has_key && !apiKey.trim();
  const dirty =
    !isActive ||
    model.trim() !== (provider.model || '') ||
    apiKey.trim().length > 0 ||
    baseUrl.trim() !== (provider.base_url || '');

  const save = async () => {
    setSaving(true);
    setTest(null);
    try {
      const body: Parameters<typeof api.updateSettings>[0] = { provider: provider.id };
      if (model.trim()) body.models = { [provider.id]: model.trim() };
      if (apiKey.trim()) body.api_keys = { [provider.id]: apiKey.trim() };
      if (baseUrl.trim() !== (provider.base_url || '')) body.base_urls = { [provider.id]: baseUrl.trim() };
      const res = await api.updateSettings(body);
      onSaved(res);
      setApiKey('');
      toast.success(res.message || 'Settings saved');
    } catch (e) {
      toast.error(api.errorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    try {
      setTest(await api.testConnection());
    } catch (e) {
      setTest({ ok: false, error: api.errorMessage(e), provider: settings.provider, model: settings.model });
    } finally {
      setTesting(false);
    }
  };

  const prepare = async () => {
    setPreparing(true);
    try {
      setLiquid(await api.prepareLiquidModel());
      toast.info('Fetching the Liquid model. It only downloads once, then works offline.');
    } catch (e) {
      toast.error(api.errorMessage(e));
      setLiquid(await api.getLiquidStatus().catch(() => liquid));
    } finally {
      setPreparing(false);
    }
  };

  return (
    <Section
      title="Model"
      icon={<Cpu className="h-4 w-4" />}
      description={
        <>
          Active: <span className="font-mono text-fg-2">{settings.provider}</span> ·{' '}
          <span className="font-mono text-fg-2">{settings.model}</span> · context ~{settings.context_window.toLocaleString()} tokens
        </>
      }
    >
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {settings.providers.map((p: ProviderInfo) => {
          const active = p.id === settings.provider;
          const sel = p.id === selected;
          return (
            <button
              key={p.id}
              onClick={() => setSelected(p.id)}
              aria-pressed={sel}
              className={cx(
                'rounded-xl border px-3 py-2.5 text-left transition-colors',
                sel ? 'border-primary/50 bg-primary/[0.07] ring-1 ring-primary/20' : 'border-line-strong bg-bg hover:border-line-strong hover:bg-elevated',
              )}
            >
              <div className="flex items-center gap-2">
                <span className="truncate text-[13px] font-semibold text-fg">{p.name}</span>
                {active && <span className="ml-auto flex-none rounded bg-teal/15 px-1.5 py-px text-[10px] font-semibold uppercase text-teal">Active</span>}
              </div>
              <p className="mt-0.5 truncate text-[11.5px] text-muted">{p.description}</p>
              <p className={cx('mt-1.5 flex items-center gap-1 text-[10.5px]', !p.needs_key ? 'text-muted' : p.has_key ? 'text-teal' : 'text-warn')}>
                {p.local ? <HardDrive className="h-3 w-3" /> : <KeyRound className="h-3 w-3" />}
                {p.local
                  ? 'Offline · no key'
                  : !p.needs_key
                    ? p.has_key
                      ? 'Key saved (optional)'
                      : 'No key needed'
                    : p.has_key
                      ? 'Key saved'
                      : 'Key required'}
              </p>
            </button>
          );
        })}
      </div>

      <div className="mt-5 space-y-4 rounded-xl border border-line bg-bg/50 p-4">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold text-fg">{provider.name}</h3>
          {isActive && <span className="text-[11px] text-teal">in use</span>}
        </div>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <div>
            <label className="label" htmlFor="model-input">
              Model
            </label>
            <input
              id="model-input"
              list={`models-${provider.id}`}
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder={provider.default_model || 'model name'}
              className="input font-mono"
            />
            <datalist id={`models-${provider.id}`}>
              {provider.models.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
            <p className="hint">Pick a suggestion or type any model id{provider.default_model ? ` (default ${provider.default_model})` : ''}.</p>
          </div>
          {keyField && (
            <div>
              <label className="label" htmlFor="key-input">
                API key {!provider.needs_key && <span className="font-normal text-muted">(optional)</span>}
              </label>
              <input
                id="key-input"
                type="password"
                autoComplete="off"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={provider.has_key ? 'Saved. Enter a new key to replace it' : 'Paste your API key'}
                className="input font-mono"
              />
              <p className={cx('hint', provider.has_key && 'text-teal')}>
                {provider.has_key ? 'A key is saved. It is never shown here.' : `Stored in ~/.alita/.env${provider.env_key ? ` as ${provider.env_key}` : ''}.`}
              </p>
            </div>
          )}
          {showUrl && (
            <div className={keyField ? 'md:col-span-2' : ''}>
              <label className="label" htmlFor="url-input">
                Base URL
              </label>
              <input id="url-input" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={provider.default_base_url} className="input font-mono" />
              <p className="hint">{provider.id === 'ollama' ? 'Where Ollama is listening.' : 'OpenAI-compatible endpoint, usually ending in /v1.'}</p>
            </div>
          )}
        </div>

        {isLocal && (
          <div className="rounded-xl border border-line-strong bg-elevated px-3.5 py-3">
            <div className="flex flex-wrap items-center gap-2">
              <HardDrive className="h-4 w-4 text-primary" />
              <p className="text-[13px] font-semibold text-fg">Offline model</p>
              <span
                className={cx(
                  'rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase',
                  liquid?.downloaded ? 'bg-teal/15 text-teal' : liquid?.state === 'error' ? 'bg-danger/15 text-danger' : 'bg-warn/15 text-warn',
                )}
              >
                {liquid?.downloaded ? 'ready' : busy ? 'downloading' : liquid?.state === 'error' ? 'error' : 'not downloaded'}
              </span>
              <span className="ml-auto font-mono text-[10.5px] text-muted-2">
                {liquid?.quant || ''} {liquid?.size_bytes ? `· ${formatBytes(liquid.size_bytes)}` : ''}
              </span>
            </div>
            <p className="mt-1 text-[11.5px] leading-relaxed text-muted">
              Fetched automatically from Hugging Face and cached in{' '}
              <span className="font-mono text-fg-2">~/.alita/models/liquid</span>. After the first download this runs with
              no internet and no API key.
            </p>
            {busy && (
              <div className="mt-2">
                <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.08]">
                  <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${Math.max(3, (liquid?.progress || 0) * 100)}%` }} />
                </div>
                <p className="mt-1 text-[10.5px] text-muted-2">
                  {liquid?.total_bytes
                    ? `${formatBytes(liquid.downloaded_bytes)} / ${formatBytes(liquid.total_bytes)}`
                    : `${formatBytes(liquid?.downloaded_bytes || 0)} downloaded`}
                </p>
              </div>
            )}
            {liquid?.runtime_error && (
              <p className="mt-2 text-[11px] leading-snug text-warn">{liquid.runtime_error}</p>
            )}
            {liquid?.error && <p className="mt-2 text-[11px] leading-snug text-danger">{liquid.error}</p>}
            <div className="mt-2.5 flex flex-wrap items-center gap-2">
              <button
                className="btn-secondary px-3 py-1.5 text-xs"
                onClick={prepare}
                disabled={preparing || busy || !liquid?.runtime_available}
                title={!liquid?.runtime_available ? liquid?.runtime_error || 'Local inference runtime unavailable' : undefined}
              >
                {preparing ? <Spinner className="h-3.5 w-3.5" /> : <Download className="h-3.5 w-3.5" />}
                {liquid?.downloaded ? 'Verify model' : 'Download model'}
              </button>
              <span className="text-[10.5px] text-muted-2">
                {liquid?.downloaded ? 'No further download needed.' : 'One-time download; about 0.8 GB for the default model.'}
              </span>
            </div>
          </div>
        )}

        {!showUrl && !isLocal && (
          <div>
            <button className="flex items-center gap-1 text-xs font-medium text-muted hover:text-fg-2" onClick={() => setAdvanced((a) => !a)} aria-expanded={advanced}>
              <ChevronRight className={cx('h-3.5 w-3.5 transition-transform', advanced && 'rotate-90')} /> Advanced
            </button>
            {advanced && (
              <div className="mt-3 max-w-xl">
                <label className="label" htmlFor="url-input-adv">
                  Base URL
                </label>
                <input id="url-input-adv" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={provider.default_base_url} className="input font-mono" />
                <p className="hint">Override for proxies or regional endpoints. Default: {provider.default_base_url}</p>
              </div>
            )}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2 border-t border-line pt-4">
          <button className="btn-primary" onClick={save} disabled={saving || missingKey || !dirty} title={missingKey ? 'An API key is required' : undefined}>
            {saving ? <Spinner className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />}
            {isActive ? 'Save' : `Save and use ${provider.name}`}
          </button>
          <button className="btn-secondary" onClick={runTest} disabled={testing} title="Sends a tiny test prompt to the active model">
            {testing ? <Spinner className="h-3.5 w-3.5" /> : <Plug className="h-3.5 w-3.5" />} Test connection
          </button>
          {missingKey && <span className="text-xs text-warn">Enter an API key to use this provider.</span>}
          {!isActive && !missingKey && <span className="text-xs text-muted">Test checks the active provider ({settings.provider}).</span>}
        </div>
        {test && (
          <div
            className={cx(
              'flex items-start gap-2 rounded-lg border px-3 py-2 text-[12.5px]',
              test.ok ? 'border-teal/30 bg-teal/[0.06] text-teal' : 'border-danger/30 bg-danger/[0.06] text-danger',
            )}
          >
            {test.ok ? <CircleCheck className="mt-0.5 h-4 w-4 flex-none" /> : <CircleX className="mt-0.5 h-4 w-4 flex-none" />}
            <div className="min-w-0">
              <p className="font-medium">
                {test.ok ? `Connected to ${test.provider} · ${test.model}` : `Connection failed (${test.provider} · ${test.model})`}
                {test.ok && test.latency_ms != null && <span className="font-normal opacity-80"> in {test.latency_ms} ms</span>}
              </p>
              {test.ok && test.reply && <p className="mt-0.5 text-fg-2">Reply: “{test.reply}”</p>}
              {!test.ok && test.error && <p className="mt-0.5 break-words text-fg-2">{test.error}</p>}
            </div>
          </div>
        )}
      </div>
    </Section>
  );
}
