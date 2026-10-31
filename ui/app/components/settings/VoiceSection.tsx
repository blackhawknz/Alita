'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AudioLines,
  Check,
  CircleCheck,
  CircleX,
  Play,
  Save,
  Search,
  Square,
  Volume2,
  VolumeX,
} from 'lucide-react';
import * as api from '@/lib/api';
import {
  VOICE_LANGUAGES,
  readConversationSettings,
  saveConversationSettings,
  voiceInputSupported,
  type ConversationSettings,
} from '@/lib/conversation';
import { cx } from '@/lib/format';
import type { Settings, VoiceInfo, VoiceProviderInfo, VoiceStatus } from '@/lib/types';
import {
  refreshVoiceStatus,
  setAutoSpeak,
  useAutoSpeak,
  useBrowserVoices,
} from '@/lib/voice';
import { useToast } from '../Toaster';
import { Section, Spinner, Toggle } from '../ui';

const MODEL_LABELS: Record<string, string> = {
  eleven_multilingual_v2: 'Multilingual v2: natural and consistent',
  eleven_flash_v2_5: 'Flash v2.5: fastest, about half the credits',
  eleven_turbo_v2_5: 'Turbo v2.5: fast, about half the credits',
  eleven_v3: 'v3: most expressive',
};

const PROVIDER_ORDER = ['edge', 'browser', 'elevenlabs', 'openai'];
const providerRank = (id: string) => {
  const i = PROVIDER_ORDER.indexOf(id);
  return i === -1 ? 99 : i;
};

/** Play a one-off preview without touching the reply playback state. */
function usePreviewAudio() {
  const ref = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);
  const stop = useCallback(() => {
    try {
      window.speechSynthesis?.cancel();
    } catch {
      /* ignore */
    }
    if (ref.current) {
      ref.current.pause();
      ref.current = null;
    }
    setPlaying(null);
  }, []);
  const playUrl = useCallback(
    async (key: string, url: string) => {
      stop();
      const el = new Audio(api.withToken(url));
      ref.current = el;
      el.onended = () => setPlaying(null);
      el.onerror = () => setPlaying(null);
      setPlaying(key);
      try {
        await el.play();
      } catch {
        setPlaying(null);
      }
    },
    [stop],
  );
  const playLocal = useCallback(
    (key: string, voiceName: string) => {
      stop();
      const synth = window.speechSynthesis;
      if (!synth) return;
      const utter = new SpeechSynthesisUtterance("Hi, I'm Alita. This is how I sound.");
      const v = synth.getVoices().find((x) => x.name === voiceName);
      if (v) utter.voice = v;
      utter.onend = () => setPlaying(null);
      setPlaying(key);
      synth.speak(utter);
    },
    [stop],
  );
  useEffect(() => stop, [stop]);
  return { playing, playUrl, playLocal, stop };
}

export default function VoiceSection({
  settings,
  onSaved,
}: {
  settings: Settings;
  onSaved: (s: Settings) => void;
}) {
  const toast = useToast();
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [voices, setVoices] = useState<VoiceInfo[]>([]);
  const [voicesLoading, setVoicesLoading] = useState(false);
  const [search, setSearch] = useState('');
  const [debounced, setDebounced] = useState('');
  const [source, setSource] = useState<'account' | 'library'>('account');
  const [savingVoice, setSavingVoice] = useState<string | null>(null);
  const [speed, setSpeed] = useState(1);
  const [stability, setStability] = useState(0.5);
  const [similarity, setSimilarity] = useState(0.75);
  const [style, setStyle] = useState(0);
  const [instructions, setInstructions] = useState('');
  const [savingTuning, setSavingTuning] = useState(false);
  const [key, setKey] = useState('');
  const [model, setModel] = useState('eleven_multilingual_v2');
  const [savingKey, setSavingKey] = useState(false);
  const [usage, setUsage] = useState<VoiceStatus['usage']>(null);
  const autoSpeak = useAutoSpeak();
  const browserVoices = useBrowserVoices();
  const preview = usePreviewAudio();
  const [conversation, setConversation] = useState<ConversationSettings>(readConversationSettings);
  const [voiceInput, setVoiceInput] = useState(false);

  useEffect(() => {
    setVoiceInput(voiceInputSupported());
    const onSettings = (event: Event) => {
      const detail = (event as CustomEvent<ConversationSettings>).detail;
      if (detail) setConversation(detail);
    };
    window.addEventListener('alita:voice-conversation', onSettings);
    return () => window.removeEventListener('alita:voice-conversation', onSettings);
  }, []);

  const patchConversation = (patch: Partial<ConversationSettings>) => {
    const next = { ...conversation, ...patch };
    setConversation(next);
    saveConversationSettings(next);
  };

  const applyStatus = useCallback((s: VoiceStatus) => {
    setStatus(s);
    setSpeed(s.settings?.speed ?? 1);
    setStability(s.settings?.stability ?? 0.5);
    setSimilarity(s.settings?.similarity_boost ?? 0.75);
    setStyle(s.settings?.style ?? 0);
    setInstructions(s.settings?.instructions ?? '');
    if (s.model) setModel(s.model);
  }, []);

  const load = useCallback(() => {
    setLoading(true);
    api
      .getVoiceStatus(true)
      .then((s) => {
        applyStatus(s);
        setUsage(s.usage ?? null);
      })
      .catch((e) => toast.error(api.errorMessage(e)))
      .finally(() => setLoading(false));
  }, [applyStatus, toast]);

  useEffect(load, [load]);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(search.trim()), 250);
    return () => clearTimeout(t);
  }, [search]);

  const provider = status?.provider;
  const providerInfo: VoiceProviderInfo | undefined = useMemo(
    () => status?.providers.find((p) => p.id === provider),
    [status, provider],
  );

  // Voices for the active provider (the browser's own list needs no request).
  useEffect(() => {
    if (!provider) return;
    if (provider === 'browser') {
      setVoices([]);
      return;
    }
    let alive = true;
    setVoicesLoading(true);
    api
      .getVoices({ provider, search: debounced, source, limit: 400 })
      .then((r) => alive && setVoices(r.voices))
      .catch((e) => alive && toast.error(api.errorMessage(e)))
      .finally(() => alive && setVoicesLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider, debounced, source]);

  const chooseProvider = async (p: VoiceProviderInfo) => {
    if (!p.available) {
      toast.error(p.error || `${p.label} isn't available yet.`);
      return;
    }
    if (p.id === provider) return;
    try {
      const res = await api.updateVoice({ provider: p.id });
      applyStatus(res.status);
      void refreshVoiceStatus();
      setSource('account');
    } catch (e) {
      toast.error(api.errorMessage(e));
    }
  };

  const chooseVoice = async (v: { voice_id: string; name?: string; public_owner_id?: string }) => {
    if (!provider) return;
    setSavingVoice(v.voice_id);
    try {
      const res = await api.updateVoice({
        provider,
        voice_id: v.voice_id,
        name: v.name,
        public_owner_id: v.public_owner_id,
      });
      applyStatus(res.status);
      void refreshVoiceStatus();
      toast.success(`Voice set to ${res.voice?.name || v.name || v.voice_id}`);
    } catch (e) {
      toast.error(api.errorMessage(e));
    } finally {
      setSavingVoice(null);
    }
  };

  const previewVoice = async (v: { voice_id: string; public_owner_id?: string; name?: string }) => {
    if (!provider) return;
    const key = `${provider}:${v.voice_id}`;
    if (preview.playing === key) {
      preview.stop();
      return;
    }
    if (provider === 'browser') {
      preview.playLocal(key, v.voice_id);
      return;
    }
    try {
      const res = await api.previewVoice({
        provider,
        voice_id: v.voice_id,
        public_owner_id: v.public_owner_id,
      });
      await preview.playUrl(key, res.audio_url);
    } catch (e) {
      toast.error(api.errorMessage(e));
    }
  };

  const saveTuning = async () => {
    if (!provider) return;
    setSavingTuning(true);
    try {
      const res = await api.updateVoice({
        provider,
        speed,
        stability: provider === 'elevenlabs' ? stability : undefined,
        similarity_boost: provider === 'elevenlabs' ? similarity : undefined,
        style: provider === 'elevenlabs' ? style : undefined,
        instructions: provider === 'openai' ? instructions : undefined,
      });
      applyStatus(res.status);
      void refreshVoiceStatus();
      toast.success('Voice settings saved');
    } catch (e) {
      toast.error(api.errorMessage(e));
    } finally {
      setSavingTuning(false);
    }
  };

  const saveKey = async () => {
    setSavingKey(true);
    try {
      const res = await api.updateSettings({
        elevenlabs_api_key: key.trim() || undefined,
        elevenlabs_model: model,
      });
      onSaved(res);
      setKey('');
      const s = await api.getVoiceStatus(true);
      applyStatus(s);
      setUsage(s.usage ?? null);
      toast.success('ElevenLabs settings saved');
    } catch (e) {
      toast.error(api.errorMessage(e));
    } finally {
      setSavingKey(false);
    }
  };

  const current = status?.voice;
  const isEleven = provider === 'elevenlabs';
  const isOpenAI = provider === 'openai';

  const rows: VoiceInfo[] = useMemo(() => {
    if (provider !== 'browser') return voices;
    const needle = debounced.toLowerCase();
    return browserVoices
      .map((v) => ({
        voice_id: v.name,
        name: v.name,
        provider: 'browser',
        locale: v.lang,
        locale_name: v.localService ? `${v.lang} · local` : v.lang,
        description: v.default ? 'Default device voice' : undefined,
        tags: [],
        gender: null,
      }))
      .filter((v) => !needle || `${v.name} ${v.locale}`.toLowerCase().includes(needle))
      .slice(0, 300);
  }, [provider, voices, browserVoices, debounced]);

  return (
    <div className="space-y-5">
      <Section
        title="Voice"
        icon={<AudioLines className="h-4 w-4" />}
        description="How Alita sounds when her replies are read aloud. She can also choose and tune her own voice in chat — just ask."
      >
        {loading && (
          <div className="h-24">
            <div className="flex h-full items-center justify-center">
              <Spinner className="h-5 w-5 text-muted" />
            </div>
          </div>
        )}

        {status && (
          <div className="space-y-5">
            {/* Providers */}
            <div>
              <p className="label">Voice service</p>
              <div className="grid gap-2 sm:grid-cols-2">
                {[...status.providers]
                  .sort((a, b) => providerRank(a.id) - providerRank(b.id))
                  .map((p) => {
                    const active = p.id === status.provider;
                    return (
                      <button
                        key={p.id}
                        onClick={() => chooseProvider(p)}
                        aria-pressed={active}
                        className={cx(
                          'rounded-xl border px-3.5 py-3 text-left transition-colors',
                          active
                            ? 'border-primary/60 bg-primary/[0.08]'
                            : 'border-line-strong bg-elevated hover:border-primary/30 hover:bg-overlay',
                          !p.available && 'opacity-60',
                        )}
                      >
                        <div className="flex items-center gap-2">
                          <span className="text-[13px] font-semibold text-fg">{p.label}</span>
                          <span
                            className={cx(
                              'rounded-full px-1.5 py-0.5 text-[10px] font-medium',
                              p.free ? 'bg-teal/15 text-teal' : 'bg-warn/15 text-warn',
                            )}
                          >
                            {p.free ? 'Free' : 'Paid'}
                          </span>
                          {active && <Check className="ml-auto h-4 w-4 text-primary" />}
                        </div>
                        <p className="mt-1 text-[11.5px] leading-snug text-muted">{p.description}</p>
                        {!p.available && p.error && (
                          <p className="mt-1 text-[11px] leading-snug text-warn">{p.error}</p>
                        )}
                      </button>
                    );
                  })}
              </div>
            </div>

            {/* Hands-free conversation */}
            <div className="rounded-xl border border-line bg-bg/40 px-3.5 py-3">
              <div className="flex flex-wrap items-center gap-3">
                <div className="min-w-0 flex-1">
                  <p className="text-[13px] font-semibold text-fg">Hands-free conversation</p>
                  <p className="mt-0.5 text-[11.5px] leading-relaxed text-muted">
                    Speak naturally; a pause sends your turn. Alita answers aloud, then listens again. Start talking while
                    she is speaking to interrupt her. Microphone input uses your browser&apos;s speech recognition (Chrome
                    or Edge) and needs localhost or HTTPS.
                  </p>
                </div>
                <Toggle
                  checked={conversation.enabled}
                  disabled={!voiceInput}
                  onChange={(v) => patchConversation({ enabled: v })}
                  label="Hands-free voice conversation"
                />
              </div>
              {!voiceInput && (
                <p className="mt-2 text-[11px] text-warn">
                  This browser doesn&apos;t expose speech recognition. You can still use the speaker buttons to hear replies.
                </p>
              )}
              <div className="mt-3 grid gap-3 border-t border-line pt-3 sm:grid-cols-3">
                <label className="block">
                  <span className="label mb-1">Spoken language</span>
                  <select
                    value={conversation.lang}
                    onChange={(e) => patchConversation({ lang: e.target.value })}
                    className="input"
                    aria-label="Speech recognition language"
                  >
                    {VOICE_LANGUAGES.map(([code, label]) => (
                      <option key={code} value={code}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block">
                  <span className="label mb-1 flex items-center justify-between">
                    End-of-turn pause <span className="font-mono text-[11px] text-muted">{(conversation.silenceMs / 1000).toFixed(1)}s</span>
                  </span>
                  <input
                    type="range"
                    min={400}
                    max={3000}
                    step={100}
                    value={conversation.silenceMs}
                    onChange={(e) => patchConversation({ silenceMs: Number(e.target.value) })}
                    className="w-full accent-[var(--primary)]"
                  />
                </label>
                <div className="flex items-end">
                  <label className="flex items-center gap-2 text-xs text-muted">
                    <Toggle
                      checked={conversation.pushToTalk}
                      onChange={(v) => patchConversation({ pushToTalk: v })}
                      label="Push-to-talk"
                    />
                    Push-to-talk
                  </label>
                </div>
              </div>
              <p className="mt-2 text-[11px] leading-relaxed text-muted-2">
                Push-to-talk: hold the microphone button in the message bar, speak, then release to send.
              </p>
            </div>

            {/* Current voice */}
            <div className="rounded-xl border border-line bg-bg/40 px-3.5 py-3">
              <div className="flex flex-wrap items-center gap-3">
                <div className="min-w-0 flex-1">
                  <p className="flex items-center gap-1.5 text-[13px] text-fg-2">
                    {current ? (
                      <>
                        <CircleCheck className="h-4 w-4 text-teal" />
                        <span>
                          Current voice: <span className="font-medium text-fg">{current.name}</span>
                        </span>
                        {current.default && (
                          <span className="rounded-full bg-white/[0.07] px-1.5 py-0.5 text-[10px] text-muted">default</span>
                        )}
                      </>
                    ) : (
                      <>
                        <CircleX className="h-4 w-4 text-muted" />
                        <span>No voice chosen for {status.provider_label} yet.</span>
                      </>
                    )}
                  </p>
                  {current?.reason && <p className="mt-0.5 text-[11.5px] italic text-muted">&ldquo;{current.reason}&rdquo;</p>}
                  <p className="mt-1 font-mono text-[10.5px] text-muted-2">
                    {status.provider_label} · speed {status.settings.speed} · replies capped at {status.max_chars} chars
                    {isEleven && ` · ${MODEL_LABELS[status.model] || status.model}`}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    className="btn-secondary px-2.5 py-1 text-xs"
                    disabled={!current || provider === 'browser'}
                    onClick={() =>
                      current && previewVoice({ voice_id: current.voice_id, name: current.name })
                    }
                    title={provider === 'browser' ? 'Use the voice list below to preview device voices' : 'Hear this voice'}
                  >
                    {preview.playing === `${provider}:${current?.voice_id}` ? (
                      <Square className="h-3 w-3 fill-current" />
                    ) : (
                      <Play className="h-3.5 w-3.5" />
                    )}
                    Preview
                  </button>
                </div>
              </div>

              {/* Tuning */}
              <div className="mt-3 grid gap-3 border-t border-line pt-3 sm:grid-cols-2">
                <label className="block">
                  <span className="label mb-1 flex items-center justify-between">
                    Speed <span className="font-mono text-[11px] text-muted">{speed.toFixed(2)}×</span>
                  </span>
                  <input
                    type="range"
                    min={isEleven ? 0.7 : 0.5}
                    max={isEleven ? 1.2 : 2}
                    step={0.05}
                    value={speed}
                    onChange={(e) => setSpeed(Number(e.target.value))}
                    className="w-full accent-[var(--primary)]"
                  />
                </label>
                {isEleven && (
                  <>
                    <label className="block">
                      <span className="label mb-1 flex items-center justify-between">
                        Stability <span className="font-mono text-[11px] text-muted">{stability.toFixed(2)}</span>
                      </span>
                      <input type="range" min={0} max={1} step={0.05} value={stability}
                        onChange={(e) => setStability(Number(e.target.value))} className="w-full accent-[var(--primary)]" />
                    </label>
                    <label className="block">
                      <span className="label mb-1 flex items-center justify-between">
                        Similarity <span className="font-mono text-[11px] text-muted">{similarity.toFixed(2)}</span>
                      </span>
                      <input type="range" min={0} max={1} step={0.05} value={similarity}
                        onChange={(e) => setSimilarity(Number(e.target.value))} className="w-full accent-[var(--primary)]" />
                    </label>
                    <label className="block">
                      <span className="label mb-1 flex items-center justify-between">
                        Style <span className="font-mono text-[11px] text-muted">{style.toFixed(2)}</span>
                      </span>
                      <input type="range" min={0} max={1} step={0.05} value={style}
                        onChange={(e) => setStyle(Number(e.target.value))} className="w-full accent-[var(--primary)]" />
                    </label>
                  </>
                )}
                {isOpenAI && (
                  <label className="block sm:col-span-2">
                    <span className="label mb-1">Tone instructions</span>
                    <input
                      value={instructions}
                      onChange={(e) => setInstructions(e.target.value)}
                      placeholder="e.g. Warm, British, unhurried and a little playful"
                      className="input"
                    />
                  </label>
                )}
                <div className="flex items-center gap-2 sm:col-span-2">
                  <button className="btn-primary px-3 py-1.5 text-xs" onClick={saveTuning} disabled={savingTuning || !current}>
                    {savingTuning ? <Spinner className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />} Save tuning
                  </button>
                  <div className="flex-1" />
                  <label className="flex items-center gap-2 text-xs text-muted">
                    <Toggle
                      checked={autoSpeak}
                      onChange={(v) => {
                        setAutoSpeak(v);
                        toast.info(v ? 'Replies will be read aloud' : 'Auto-speak off');
                      }}
                      label="Read replies aloud automatically"
                    />
                    Read replies aloud
                  </label>
                </div>
              </div>
            </div>

            {/* Voice list */}
            <div>
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <div className="relative min-w-[200px] flex-1">
                  <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
                  <input
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder={provider === 'browser' ? 'Search device voices' : 'Search voices by name or locale'}
                    className="input pl-8"
                    aria-label="Search voices"
                  />
                </div>
                {isEleven && (
                  <div className="flex overflow-hidden rounded-lg border border-line-strong text-xs">
                    {(['account', 'library'] as const).map((s) => (
                      <button
                        key={s}
                        onClick={() => setSource(s)}
                        className={cx('px-2.5 py-1.5', source === s ? 'bg-primary/15 text-primary' : 'text-muted hover:text-fg-2')}
                      >
                        {s === 'account' ? 'My voices' : 'Library'}
                      </button>
                    ))}
                  </div>
                )}
                {voicesLoading && <Spinner className="h-4 w-4 text-muted" />}
              </div>
              {source === 'library' && isEleven && (
                <p className="mb-2 text-[11px] leading-relaxed text-muted">
                  Choosing a library voice adds it to your ElevenLabs account and uses one of your voice slots.
                </p>
              )}
              <div className="max-h-[380px] overflow-y-auto rounded-xl border border-line">
                {rows.length === 0 && !voicesLoading && (
                  <p className="px-3 py-6 text-center text-xs text-muted">
                    {provider === 'browser'
                      ? 'No device voices found. Your browser or OS may not expose any.'
                      : 'No voices match.'}
                  </p>
                )}
                {rows.map((v) => {
                  const isCurrent =
                    current?.provider === provider && current?.voice_id === v.voice_id;
                  const pkey = `${provider}:${v.voice_id}`;
                  const meta = [v.locale_name || v.locale, v.gender, ...(v.tags || [])].filter(Boolean).join(' · ');
                  return (
                    <div
                      key={v.voice_id}
                      className={cx(
                        'flex items-center gap-2 border-b border-line/60 px-3 py-2 last:border-b-0',
                        isCurrent && 'bg-primary/[0.06]',
                      )}
                    >
                      <button
                        className="icon-btn h-7 w-7 flex-none"
                        onClick={() => previewVoice({ voice_id: v.voice_id, public_owner_id: v.public_owner_id, name: v.name })}
                        title={`Preview ${v.name}`}
                        aria-label={`Preview ${v.name}`}
                      >
                        {preview.playing === pkey ? (
                          <Square className="h-3 w-3 fill-current" />
                        ) : (
                          <Play className="h-3.5 w-3.5" />
                        )}
                      </button>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-[12.5px] text-fg-2">
                          {v.name}
                          {isCurrent && <span className="ml-1.5 text-[10px] text-primary">current</span>}
                        </p>
                        <p className="truncate text-[10.5px] text-muted-2">{meta}</p>
                      </div>
                      <button
                        className={cx('flex-none rounded-lg px-2 py-1 text-[11px] font-medium',
                          isCurrent ? 'text-muted' : 'text-primary hover:bg-primary/10')}
                        disabled={isCurrent || savingVoice !== null}
                        onClick={() => chooseVoice(v)}
                      >
                        {savingVoice === v.voice_id ? <Spinner className="h-3 w-3" /> : isCurrent ? 'In use' : 'Use'}
                      </button>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* ElevenLabs credentials */}
            {isEleven && (
              <div className="rounded-xl border border-line px-3.5 py-3">
                <p className="mb-2 flex items-center gap-1.5 text-[12.5px] font-medium text-fg-2">
                  {providerInfo?.available ? (
                    <CircleCheck className="h-4 w-4 text-teal" />
                  ) : (
                    <CircleX className="h-4 w-4 text-muted" />
                  )}
                  ElevenLabs account
                  {usage?.character_limit ? (
                    <span className="ml-auto font-mono text-[10.5px] font-normal text-muted">
                      {(usage.characters_used ?? 0).toLocaleString()} / {usage.character_limit.toLocaleString()} credits
                    </span>
                  ) : null}
                </p>
                <div className="grid gap-2 sm:grid-cols-2">
                  <input
                    type="password"
                    value={key}
                    onChange={(e) => setKey(e.target.value)}
                    placeholder={providerInfo?.available ? 'Key is set. Paste a new one to replace it.' : 'ElevenLabs API key'}
                    className="input font-mono"
                    aria-label="ElevenLabs API key"
                    autoComplete="off"
                  />
                  <select value={model} onChange={(e) => setModel(e.target.value)} className="input" aria-label="Speech model">
                    {(providerInfo?.models || []).map((m) => (
                      <option key={m} value={m}>
                        {MODEL_LABELS[m] || m}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="mt-2 flex items-center gap-2">
                  <button className="btn-secondary px-3 py-1.5 text-xs" onClick={saveKey} disabled={savingKey}>
                    {savingKey ? <Spinner className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />} Save account
                  </button>
                  <span className="text-[10.5px] text-muted-2">Stored in backend/.env. Never shown again.</span>
                </div>
              </div>
            )}

            <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-muted-2">
              {autoSpeak ? <Volume2 className="mt-0.5 h-3.5 w-3.5 flex-none text-primary" /> : <VolumeX className="mt-0.5 h-3.5 w-3.5 flex-none" />}
              Use the speaker button under a reply to hear it. With auto-speak on, new replies are read aloud when they finish; the
              toggle is also in the chat header. Alita can pick and tune her voice herself — try &ldquo;choose a warm British voice&rdquo; in chat.
            </p>
          </div>
        )}
      </Section>
    </div>
  );
}
