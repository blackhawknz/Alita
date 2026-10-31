'use client';

// Hands-free voice conversation. The browser transcribes speech locally
// (Web Speech API), silence ends the turn, and Alita's spoken reply hands the
// floor back automatically. Push-to-talk is available as an alternative.

import { useCallback, useEffect, useRef, useState } from 'react';
import { getPlaybackState, setAutoSpeak, subscribePlayback } from './voice';

export type VoicePhase = 'off' | 'listening' | 'thinking' | 'speaking' | 'error';

export interface ConversationSettings {
  enabled: boolean;
  lang: string;
  silenceMs: number;
  pushToTalk: boolean;
}

const STORAGE_KEY = 'alita:voice-conversation';
const DEFAULTS: ConversationSettings = {
  enabled: false,
  lang: 'en-US',
  silenceMs: 1400,
  pushToTalk: false,
};

export const VOICE_LANGUAGES = [
  ['en-US', 'English (US)'],
  ['en-GB', 'English (UK)'],
  ['en-AU', 'English (Australia)'],
  ['en-IN', 'English (India)'],
  ['fr-FR', 'French'],
  ['de-DE', 'German'],
  ['es-ES', 'Spanish'],
  ['it-IT', 'Italian'],
  ['pt-BR', 'Portuguese (Brazil)'],
  ['ja-JP', 'Japanese'],
  ['ko-KR', 'Korean'],
  ['zh-CN', 'Chinese (Mandarin)'],
] as const;

export function readConversationSettings(): ConversationSettings {
  if (typeof window === 'undefined') return DEFAULTS;
  try {
    const raw = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || '{}') as Partial<ConversationSettings>;
    return {
      enabled: !!raw.enabled,
      lang: typeof raw.lang === 'string' && raw.lang ? raw.lang : DEFAULTS.lang,
      silenceMs: typeof raw.silenceMs === 'number' ? Math.min(3000, Math.max(400, raw.silenceMs)) : DEFAULTS.silenceMs,
      pushToTalk: !!raw.pushToTalk,
    };
  } catch {
    return DEFAULTS;
  }
}

export function saveConversationSettings(settings: ConversationSettings) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  } catch {
    /* storage unavailable */
  }
  window.dispatchEvent(new CustomEvent('alita:voice-conversation', { detail: settings }));
}

interface RecognitionAlternative {
  transcript: string;
}

interface RecognitionResult {
  isFinal: boolean;
  length: number;
  [index: number]: RecognitionAlternative;
}

interface RecognitionResultList {
  length: number;
  [index: number]: RecognitionResult;
}

interface RecognitionEvent {
  resultIndex: number;
  results: RecognitionResultList;
}

interface RecognitionErrorEvent {
  error: string;
}

interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((event: RecognitionEvent) => void) | null;
  onerror: ((event: RecognitionErrorEvent) => void) | null;
  onend: (() => void) | null;
  onstart: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

type RecognitionCtor = new () => SpeechRecognitionLike;

function recognitionCtor(): RecognitionCtor | null {
  if (typeof window === 'undefined') return null;
  const w = window as unknown as {
    SpeechRecognition?: RecognitionCtor;
    webkitSpeechRecognition?: RecognitionCtor;
  };
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

export function voiceInputSupported(): boolean {
  return !!recognitionCtor();
}

interface Options {
  running: boolean;
  onUtterance: (text: string) => void | Promise<void>;
  onError?: (message: string) => void;
}

export function useVoiceConversation({ running, onUtterance, onError }: Options) {
  const [supported, setSupported] = useState(false);
  const [settings, setSettings] = useState<ConversationSettings>(DEFAULTS);
  const [phase, setPhaseState] = useState<VoicePhase>('off');
  const [interim, setInterim] = useState('');
  const [error, setError] = useState<string | null>(null);

  const optsRef = useRef({ running, onUtterance, onError });
  optsRef.current = { running, onUtterance, onError };
  const settingsRef = useRef<ConversationSettings>(DEFAULTS);
  const enabledRef = useRef(false);
  const runningRef = useRef(running);
  runningRef.current = running;
  const phaseRef = useRef<VoicePhase>('off');
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const finalRef = useRef('');
  const interimRef = useRef('');
  const manualRef = useRef(false);
  const suppressRef = useRef(false);
  const sendingRef = useRef(false);
  const silenceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const restartTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const apiRef = useRef<{
    begin: (manual?: boolean) => void;
    pause: () => void;
    result: (event: RecognitionEvent) => void;
    error: (event: RecognitionErrorEvent) => void;
    end: () => void;
  } | null>(null);

  const setPhase = useCallback((next: VoicePhase) => {
    phaseRef.current = next;
    setPhaseState(next);
  }, []);

  const clearTimers = useCallback(() => {
    if (silenceTimer.current) clearTimeout(silenceTimer.current);
    if (restartTimer.current) clearTimeout(restartTimer.current);
    silenceTimer.current = null;
    restartTimer.current = null;
  }, []);

  const resetTranscript = useCallback(() => {
    finalRef.current = '';
    interimRef.current = '';
    setInterim('');
  }, []);

  const submit = useCallback(async () => {
    if (sendingRef.current) return;
    clearTimers();
    const text = (finalRef.current || interimRef.current).trim();
    resetTranscript();
    if (!text) {
      if (enabledRef.current && !runningRef.current) apiRef.current?.begin(manualRef.current);
      return;
    }
    sendingRef.current = true;
    suppressRef.current = true;
    try {
      recognitionRef.current?.abort();
    } catch {
      /* not running */
    }
    setPhase('thinking');
    try {
      await optsRef.current.onUtterance(text);
    } catch (e) {
      optsRef.current.onError?.(e instanceof Error ? e.message : 'Could not send the voice message.');
    } finally {
      sendingRef.current = false;
      setTimeout(() => {
        if (enabledRef.current && !runningRef.current && !getPlaybackState().key) {
          setPhase('listening');
          apiRef.current?.begin(manualRef.current);
        }
      }, 700);
    }
  }, [clearTimers, resetTranscript, setPhase]);

  const begin = useCallback(
    (manual = false) => {
      const recognition = recognitionRef.current;
      if (!recognition || sendingRef.current) return;
      if (!enabledRef.current && !manual) return;
      if (runningRef.current) return;
      manualRef.current = manual;
      suppressRef.current = false;
      clearTimers();
      resetTranscript();
      setError(null);
      recognition.lang = settingsRef.current.lang;
      setPhase('listening');
      try {
        recognition.start();
      } catch {
        /* already started */
      }
    },
    [clearTimers, resetTranscript, setPhase],
  );

  const pause = useCallback(() => {
    clearTimers();
    suppressRef.current = true;
    try {
      recognitionRef.current?.abort();
    } catch {
      /* not running */
    }
  }, [clearTimers]);

  const scheduleSubmit = useCallback(() => {
    if (silenceTimer.current) clearTimeout(silenceTimer.current);
    const text = (finalRef.current + interimRef.current).trim();
    if (!text) return;
    silenceTimer.current = setTimeout(() => {
      silenceTimer.current = null;
      void submit();
    }, settingsRef.current.silenceMs);
  }, [submit]);

  const result = useCallback(
    (event: RecognitionEvent) => {
      if (suppressRef.current || (!enabledRef.current && !manualRef.current)) return;
      let final = '';
      let partial = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const item = event.results[i];
        const transcript = item[0]?.transcript || '';
        if (item.isFinal) final += transcript;
        else partial += transcript;
      }
      if (final) finalRef.current = `${finalRef.current} ${final}`.trim();
      interimRef.current = partial.trim();
      const display = `${finalRef.current} ${interimRef.current}`.trim();
      setInterim(display);
      if (display) {
        setPhase('listening');
        scheduleSubmit();
      }
    },
    [scheduleSubmit, setPhase],
  );

  const recognitionError = useCallback(
    (event: RecognitionErrorEvent) => {
      const code = event.error || 'unknown';
      if (code === 'no-speech' || code === 'aborted') return;
      const message =
        code === 'not-allowed' || code === 'service-not-allowed'
          ? 'Microphone access was blocked. Allow it in the browser to use voice mode.'
          : code === 'network'
            ? 'Speech recognition could not reach the browser speech service.'
            : `Voice input failed: ${code}`;
      const next = { ...settingsRef.current, enabled: false };
      settingsRef.current = next;
      enabledRef.current = false;
      setSettings(next);
      try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* storage unavailable */
      }
      setError(message);
      setPhase('error');
      optsRef.current.onError?.(message);
    },
    [setPhase],
  );

  const end = useCallback(() => {
    if (suppressRef.current) {
      suppressRef.current = false;
      return;
    }
    if ((finalRef.current || interimRef.current).trim()) {
      void submit();
      return;
    }
    if (!enabledRef.current || runningRef.current) return;
    if (restartTimer.current) clearTimeout(restartTimer.current);
    restartTimer.current = setTimeout(() => {
      restartTimer.current = null;
      apiRef.current?.begin(manualRef.current);
    }, 300);
  }, [submit]);

  apiRef.current = { begin, pause, result, error: recognitionError, end };

  useEffect(() => {
    setSupported(voiceInputSupported());
    const saved = readConversationSettings();
    settingsRef.current = saved;
    enabledRef.current = saved.enabled;
    setSettings(saved);
    setPhase(saved.enabled ? 'listening' : 'off');
  }, [setPhase]);

  useEffect(() => {
    const Ctor = recognitionCtor();
    if (!Ctor) return;
    const recognition = new Ctor();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = settingsRef.current.lang;
    recognition.onresult = (event) => apiRef.current?.result(event);
    recognition.onerror = (event) => apiRef.current?.error(event);
    recognition.onend = () => apiRef.current?.end();
    recognitionRef.current = recognition;
    return () => {
      recognition.onresult = null;
      recognition.onerror = null;
      recognition.onend = null;
      try {
        recognition.abort();
      } catch {
        /* already stopped */
      }
      recognitionRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!supported) return;
    const onSettings = (event: Event) => {
      const detail = (event as CustomEvent<ConversationSettings>).detail;
      if (!detail) return;
      settingsRef.current = detail;
      setSettings(detail);
      enabledRef.current = detail.enabled;
      if (detail.enabled) {
        setAutoSpeak(true);
        begin(false);
      } else {
        pause();
        resetTranscript();
        setPhase('off');
      }
    };
    window.addEventListener('alita:voice-conversation', onSettings);
    return () => window.removeEventListener('alita:voice-conversation', onSettings);
  }, [supported, begin, pause, resetTranscript, setPhase]);

  useEffect(() => {
    if (!supported) return;
    if (running) {
      pause();
      resetTranscript();
      setPhase('thinking');
      return;
    }
    if (!enabledRef.current) return;
    const timer = setTimeout(() => {
      if (getPlaybackState().key) {
        setPhase('speaking');
      } else {
        setPhase('listening');
        begin(manualRef.current);
      }
    }, 350);
    return () => clearTimeout(timer);
  }, [running, supported, pause, resetTranscript, setPhase, begin]);

  useEffect(() => {
    if (!supported) return;
    const unsubscribe = subscribePlayback(() => {
      if (!enabledRef.current) return;
      const state = getPlaybackState();
      if (state.key) {
        pause();
        setPhase(state.state === 'loading' ? 'thinking' : 'speaking');
      } else if (!runningRef.current) {
        setPhase('listening');
        begin(manualRef.current);
      }
    });
    return () => {
      unsubscribe();
    };
  }, [supported, pause, setPhase, begin]);

  const update = useCallback(
    (patch: Partial<ConversationSettings>) => {
      const next = { ...settingsRef.current, ...patch };
      settingsRef.current = next;
      enabledRef.current = next.enabled;
      setSettings(next);
      setError(null);
      if (next.enabled) setAutoSpeak(true);
      saveConversationSettings(next);
    },
    [],
  );

  const toggle = useCallback(() => {
    if (!supported) {
      optsRef.current.onError?.('Voice conversation needs a browser with speech recognition (Chrome or Edge).');
      return;
    }
    update({ enabled: !settingsRef.current.enabled });
  }, [supported, update]);

  const startPushToTalk = useCallback(() => {
    if (!supported) {
      optsRef.current.onError?.('Voice input needs a browser with speech recognition (Chrome or Edge).');
      return;
    }
    setAutoSpeak(true);
    begin(true);
  }, [supported, begin]);

  const stopPushToTalk = useCallback(() => {
    if (phaseRef.current !== 'listening') return;
    void submit();
  }, [submit]);

  return {
    supported,
    enabled: settings.enabled,
    settings,
    phase,
    interim,
    error,
    toggle,
    update,
    startPushToTalk,
    stopPushToTalk,
    start: begin,
    stop: () => {
      pause();
      resetTranscript();
      setPhase('off');
    },
  };
}

export type VoiceConversation = ReturnType<typeof useVoiceConversation>;
