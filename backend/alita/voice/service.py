"""
Alita's speaking voice.

Two halves share one :class:`VoiceService`:

* The ``voice`` **tool** lets Alita choose her own voice: list voices from any
  configured provider, preview them for the user, and ``set`` one (optionally
  tuning speed / stability / style). The choice persists in
  ``~/.alita/voice.json``.
* The **speech API** turns reply text into audio in the chosen voice.
  ``prepare()`` registers an utterance and returns a URL; the first GET of that
  URL generates the audio (streaming when the provider supports it) while
  teeing it into an on-disk cache, so replays are free and concurrent requests
  share one generation.

Providers (see :mod:`alita.voice.providers`): Edge neural voices (free, no
key), the browser's system voices (offline), ElevenLabs (premium) and OpenAI
TTS (cheap). ``voice.json`` stores ``provider`` plus a per-provider choice, so
switching back to a provider restores the voice chosen there.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

import httpx

from ..core import config
from .providers import TTSProvider, VoiceError, build_providers
from .text import speakable_text, truncate_speech

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 2500
PREVIEW_MAX_CHARS = 300
PREVIEW_SAMPLE = "Hi, I'm Alita. This is how I sound."
CACHE_MAX_FILES = 400
DEFAULT_SETTINGS: Dict[str, Any] = {
    "stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "speed": 1.0,
    "use_speaker_boost": True, "instructions": "",
}
_SETTING_BOUNDS = {"stability": (0.0, 1.0), "similarity_boost": (0.0, 1.0), "style": (0.0, 1.0), "speed": (0.5, 2.0)}
_UTTERANCE_ID = re.compile(r"^[a-f0-9]{40}$")
_EDGE_VOICE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _clamp_settings(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    out = {**DEFAULT_SETTINGS, **{k: v for k, v in (base or {}).items() if k in DEFAULT_SETTINGS}}
    for key, value in updates.items():
        if value is None or key not in DEFAULT_SETTINGS:
            continue
        if key == "use_speaker_boost":
            out[key] = bool(value)
            continue
        if key == "instructions":
            out[key] = str(value)[:600]
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise VoiceError(f"{key} must be a number")
        low, high = _SETTING_BOUNDS[key]
        out[key] = round(min(high, max(low, number)), 3)
    return out


class _Job:
    """One upstream generation, readable by any number of concurrent clients."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.done = False
        self.error: Optional[str] = None
        self.started = asyncio.Event()
        self.cond = asyncio.Condition()


class VoiceService:
    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.providers: Dict[str, TTSProvider] = build_providers(transport)
        self._pending: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._jobs: Dict[str, _Job] = {}
        self._errors: "OrderedDict[str, str]" = OrderedDict()
        self._tasks: set = set()

    # ─── Configuration ────────────────────────────────────────────

    def provider(self, provider_id: Optional[str] = None) -> TTSProvider:
        pid = (provider_id or self._prefs()["provider"]).lower()
        prov = self.providers.get(pid)
        if prov is None:
            raise VoiceError(f"Unknown voice provider '{pid}'. Options: {', '.join(self.providers)}")
        return prov

    @property
    def configured(self) -> bool:
        try:
            prov = self.provider()
        except VoiceError:
            return False
        return prov.available() and bool(self.current_voice())

    def _default_provider_id(self) -> str:
        edge = self.providers["edge"]
        return "edge" if edge.available() else "browser"

    @staticmethod
    def prefs_path() -> Path:
        return config.alita_home() / "voice.json"

    @staticmethod
    def cache_dir() -> Path:
        p = config.alita_home() / "voice_cache"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _prefs(self) -> Dict[str, Any]:
        """Stored preferences, normalised (and migrated from the v1 format)."""
        try:
            data = json.loads(self.prefs_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if "provider_voices" not in data and data.get("voice_id"):
            # v1 stored a single ElevenLabs choice.
            data = {
                "provider": "elevenlabs",
                "provider_voices": {"elevenlabs": {"voice_id": data["voice_id"], "name": data.get("name")}},
                "settings": data.get("settings") or {},
                "reason": data.get("reason"), "chosen_at": data.get("chosen_at"),
                "updated_at": data.get("updated_at"),
            }
        pid = str(data.get("provider") or "").lower()
        if pid not in self.providers:
            pid = self._default_provider_id()
        voices = data.get("provider_voices")
        return {
            "provider": pid,
            "provider_voices": voices if isinstance(voices, dict) else {},
            "settings": _clamp_settings(data.get("settings") or {}, {}),
            "reason": data.get("reason"),
            "chosen_at": data.get("chosen_at"),
            "updated_at": data.get("updated_at"),
        }

    def _save(self, prefs: Dict[str, Any]) -> None:
        path = self.prefs_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(prefs, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def current_voice(self) -> Optional[Dict[str, Any]]:
        """The voice that would speak now (explicit choice, else the provider's default)."""
        prefs = self._prefs()
        prov = self.providers[prefs["provider"]]
        entry = prefs["provider_voices"].get(prov.id) or {}
        voice_id = entry.get("voice_id") or prov.default_voice_id
        if not voice_id:
            return None
        return {
            "voice_id": voice_id,
            "name": entry.get("name") or (prov.default_voice_name if voice_id == prov.default_voice_id else None) or voice_id,
            "provider": prov.id,
            "settings": prefs["settings"],
            "reason": prefs.get("reason"),
            "chosen_at": prefs.get("chosen_at"),
            "default": not bool(entry.get("voice_id")),
        }

    def status(self) -> Dict[str, Any]:
        prefs = self._prefs()
        prov = self.providers[prefs["provider"]]
        voice = self.current_voice()
        return {
            "configured": prov.available() and bool(voice),
            "provider": prov.id,
            "provider_label": prov.label,
            "free": prov.free,
            "model": self._model_label(prov),
            "max_chars": prov.max_chars,
            "voice": voice,
            "settings": prefs["settings"],
            "providers": [p.info() for p in self.providers.values()],
        }

    def _model_label(self, prov: TTSProvider) -> str:
        model = getattr(prov, "model", None)
        return str(model) if model else "natural"

    async def usage(self) -> Optional[Dict[str, Any]]:
        prov = self.providers["elevenlabs"]
        return await prov.usage() if hasattr(prov, "usage") else None  # type: ignore[attr-defined]

    # ─── Voices ───────────────────────────────────────────────────

    async def list_voices(self, provider: Optional[str] = None, search: str = "", locale: str = "",
                          gender: str = "", limit: int = 100, source: str = "account") -> Dict[str, Any]:
        prov = self.provider(provider)
        if not prov.available():
            raise VoiceError(prov.availability_error() or f"{prov.label} isn't available.", 503)
        current = self.current_voice()
        voices = await prov.list_voices(search=search, locale=locale, gender=gender, limit=limit, source=source)
        out = []
        for v in voices:
            d = v.to_dict()
            if current and current["provider"] == prov.id and current["voice_id"] == v.id:
                d["current"] = True
            out.append(d)
        result: Dict[str, Any] = {
            "success": True, "provider": prov.id, "provider_label": prov.label,
            "source": source, "count": len(out), "voices": out,
        }
        if prov.id == "browser":
            result["note"] = "Browser voices are listed by the UI from the operating system."
        return result

    async def set_voice(self, provider: Optional[str] = None, voice_id: Optional[str] = None,
                        name: Optional[str] = None, reason: Optional[str] = None,
                        public_owner_id: str = "", allow_empty: bool = False,
                        **settings: Any) -> Dict[str, Any]:
        prefs = self._prefs()
        prov = self.provider(provider or prefs["provider"])
        if not prov.available():
            raise VoiceError(prov.availability_error() or f"{prov.label} isn't available.", 503)

        voice_id = (voice_id or (prefs["provider_voices"].get(prov.id) or {}).get("voice_id")
                    or prov.default_voice_id or "").strip()
        if not voice_id:
            if allow_empty:
                # The picker may switch provider before a voice is chosen.
                prefs["provider"] = prov.id
                prefs["settings"] = _clamp_settings(prefs["settings"], settings)
                prefs["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self._save(prefs)
                return {"success": True, "switched": True, "voice": None, "provider": prov.id,
                        "message": f"Switched to {prov.label}. Choose a voice to finish."}
            raise VoiceError("Choose a voice first (list them, or give a voice_id).", 400)
        if prov.id == "edge" and not _EDGE_VOICE.match(voice_id):
            raise VoiceError("That doesn't look like an Edge voice id (e.g. en-GB-SoniaNeural).")

        resolved = await prov.resolve_voice(voice_id, public_owner_id)
        new_id = resolved.get("voice_id") or voice_id
        same_voice = (prefs["provider"] == prov.id
                      and (prefs["provider_voices"].get(prov.id) or {}).get("voice_id") == new_id)

        prefs["provider"] = prov.id
        prefs["provider_voices"][prov.id] = {
            "voice_id": new_id,
            "name": resolved.get("name") or name or new_id,
        }
        prefs["settings"] = _clamp_settings(prefs["settings"], settings)
        if prov.id == "elevenlabs":  # the API accepts 0.7-1.2
            prefs["settings"]["speed"] = round(min(1.2, max(0.7, float(prefs["settings"]["speed"]))), 3)
        if reason:
            prefs["reason"] = reason.strip()[:500]
        if not same_voice:
            prefs["chosen_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        prefs["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._save(prefs)
        voice = self.current_voice()
        message = (f"Tuned your voice ({voice['name']})." if same_voice
                   else f"Your voice is now {voice['name']} ({prov.label}). Replies the user plays will use it.")
        return {"success": True, "message": message, "provider": prov.id, "voice": voice,
                "added_from_library": bool(public_owner_id)}

    async def preview(self, provider: Optional[str] = None, voice_id: Optional[str] = None,
                      text: str = "", public_owner_id: str = "") -> Dict[str, Any]:
        prefs = self._prefs()
        prov = self.provider(provider or prefs["provider"])
        if not prov.available():
            raise VoiceError(prov.availability_error() or f"{prov.label} isn't available.", 503)
        voice_id = (voice_id or (prefs["provider_voices"].get(prov.id) or {}).get("voice_id")
                    or prov.default_voice_id or "").strip()
        if not voice_id:
            raise VoiceError("Give a voice_id to preview.", 400)
        if prov.id == "browser":
            raise VoiceError("Browser voices are previewed locally by the UI.", 400)

        if public_owner_id and prov.id == "elevenlabs":
            # A public library voice isn't in the account yet: only its stock sample is free to hear.
            lib = await prov.library_voice(voice_id, public_owner_id)  # type: ignore[attr-defined]
            if not lib:
                raise VoiceError("That library voice wasn't found for this owner.", 404)
            if text:
                raise VoiceError("Library voices can only be previewed with their stock sample until you set them. "
                                 "Omit `text`.", 400)
            if not lib.get("preview_url"):
                raise VoiceError("This library voice has no stock sample.", 400)
            return {"success": True, "provider": prov.id, "voice": lib.get("name"),
                    "audio_url": lib["preview_url"], "kind": "stock_sample"}

        if text:
            spec = {"voice_id": voice_id, "name": voice_id, "provider": prov.id,
                    "settings": prefs["settings"]}
            utterance = self.prepare(text, voice=spec, limit=PREVIEW_MAX_CHARS)
            return {"success": True, "provider": prov.id, "voice": voice_id, "audio_url": utterance["url"],
                    "kind": "spoken_line", "chars": utterance["chars"], "truncated": utterance["truncated"]}

        stock = await prov.stock_preview(voice_id)
        if stock:
            return {"success": True, "provider": prov.id, "voice": voice_id, "audio_url": stock,
                    "kind": "stock_sample"}
        if prov.free or prov.id == "openai":
            # Generate a short sample (free on Edge, negligible on OpenAI).
            spec = {"voice_id": voice_id, "name": voice_id, "provider": prov.id, "settings": prefs["settings"]}
            utterance = self.prepare(PREVIEW_SAMPLE, voice=spec, limit=PREVIEW_MAX_CHARS)
            return {"success": True, "provider": prov.id, "voice": voice_id, "audio_url": utterance["url"],
                    "kind": "sample"}
        raise VoiceError("This voice has no stock sample; pass `text` to hear it speak (uses credits).", 400)

    # ─── Speech ───────────────────────────────────────────────────

    def prepare(self, text: str, *, voice: Optional[Dict[str, Any]] = None,
                limit: Optional[int] = None) -> Dict[str, Any]:
        """Register an utterance; returns its id/url. Nothing is generated until it is fetched."""
        voice = voice or self.current_voice()
        if not voice:
            raise VoiceError("Alita hasn't chosen a voice yet. Choose one in Settings → Voice, or ask her to pick.", 409)
        prov = self.provider(voice.get("provider"))
        if not prov.available():
            raise VoiceError(prov.availability_error() or f"{prov.label} isn't available.", 503)
        if prov.id == "browser":
            raise VoiceError("Browser voices are spoken locally by the UI, not the server.")
        cap = min(prov.max_chars, limit or prov.max_chars)
        spoken, truncated = truncate_speech(speakable_text(text), cap)
        if not spoken:
            raise VoiceError("Nothing to say: the message has no speakable text.", 400)
        settings = _clamp_settings(voice.get("settings") or {}, {})
        if prov.id == "elevenlabs":  # API limits
            settings["speed"] = min(1.2, max(0.7, float(settings["speed"])))
        spec = {"provider": prov.id, "voice_id": voice["voice_id"], "model": self._model_label(prov),
                "settings": settings, "text": spoken}
        uid = hashlib.sha256(json.dumps(spec, sort_keys=True).encode("utf-8")).hexdigest()[:40]
        self._pending[uid] = spec
        self._pending.move_to_end(uid)
        while len(self._pending) > 500:
            self._pending.popitem(last=False)
        return {"id": uid, "url": f"/api/voice/speech/{uid}.mp3", "chars": len(spoken),
                "truncated": truncated, "voice": voice.get("name"), "provider": prov.id}

    def cached_path(self, uid: str) -> Optional[Path]:
        if not _UTTERANCE_ID.match(uid or ""):
            return None
        path = self.cache_dir() / f"{uid}.mp3"
        return path if path.is_file() and path.stat().st_size > 0 else None

    def speech_state(self, uid: str) -> Dict[str, Any]:
        if self.cached_path(uid):
            return {"state": "ready"}
        if uid in self._jobs:
            return {"state": "generating"}
        if uid in self._errors:
            return {"state": "error", "error": self._errors[uid]}
        if uid in self._pending:
            return {"state": "pending"}
        return {"state": "unknown"}

    async def stream(self, uid: str) -> AsyncIterator[bytes]:
        """Start (or join) generation of an utterance and return a byte iterator."""
        if not _UTTERANCE_ID.match(uid or ""):
            raise VoiceError("Unknown utterance", 404)
        job = self._jobs.get(uid)
        if job is None:
            spec = self._pending.get(uid)
            if spec is None:
                raise VoiceError("Unknown or expired utterance", 404)
            self._errors.pop(uid, None)
            job = self._jobs[uid] = _Job()
            self._spawn(self._generate(uid, spec, job))
        await job.started.wait()
        if job.error and not job.buf:
            raise VoiceError(job.error, 502)
        return self._read(job)

    @staticmethod
    async def _read(job: _Job) -> AsyncIterator[bytes]:
        offset = 0
        while True:
            async with job.cond:
                await job.cond.wait_for(lambda: len(job.buf) > offset or job.done)
                chunk = bytes(job.buf[offset:])
                finished = job.done
            if chunk:
                offset += len(chunk)
                yield chunk
            elif finished:
                return

    async def _generate(self, uid: str, spec: Dict[str, Any], job: _Job) -> None:
        try:
            prov = self.provider(spec["provider"])
            started = False
            async for chunk in prov.synth(spec["text"], spec["voice_id"], spec["settings"]):
                if not started:
                    job.started.set()
                    started = True
                if chunk:
                    async with job.cond:
                        job.buf.extend(chunk)
                        job.cond.notify_all()
            if job.buf:
                job.started.set()
                self._write_cache(uid, bytes(job.buf))
            elif not started:
                job.error = f"{prov.label} returned no audio."
        except VoiceError as exc:
            job.error = str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("voice generation failed: %s", exc)
            job.error = f"Speech generation failed: {exc}"
        finally:
            async with job.cond:
                job.done = True
                job.cond.notify_all()
            job.started.set()
            if job.error:
                self._errors[uid] = job.error
                while len(self._errors) > 100:
                    self._errors.popitem(last=False)
            self._jobs.pop(uid, None)

    def _write_cache(self, uid: str, data: bytes) -> None:
        directory = self.cache_dir()
        tmp = directory / f".{uid}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, directory / f"{uid}.mp3")
        files = sorted(directory.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[CACHE_MAX_FILES:]:
            old.unlink(missing_ok=True)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        for prov in self.providers.values():
            close = getattr(prov, "aclose", None)
            if close:
                await close()

    # ─── Tool ─────────────────────────────────────────────────────

    @property
    def definition(self) -> Dict[str, Any]:
        return {
            "name": "voice",
            "description": (
                "Your own speaking voice. The user can hear your replies read aloud in the voice you choose — "
                "it's yours to pick and change. Providers: 'edge' (free neural voices, no key), 'elevenlabs' "
                "(best quality, credits), 'openai' (cheap), 'browser' (the device's local voices). `providers` "
                "shows what's available; `current` shows your voice and settings. `list` browses voices (filter "
                "with search/gender/accent/locale). `preview` plays one for the user — you can't hear it yourself, "
                "so ask what they think if you want a second opinion. `set` makes a voice yours (pass provider and "
                "voice_id) and/or tunes it: speed (0.5-2.0 for edge/openai, 0.7-1.2 for elevenlabs), stability "
                "(0-1, lower = more expressive), similarity_boost, style (0-1), and for OpenAI `instructions` "
                "(a short tone description like 'warm, British, unhurried')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["current", "providers", "list", "preview", "set"]},
                    "provider": {"type": "string", "enum": ["edge", "browser", "elevenlabs", "openai"],
                                 "description": "Voice service (default: the active one)."},
                    "source": {"type": "string", "enum": ["account", "library"],
                               "description": "ElevenLabs only: your voices or the public library."},
                    "search": {"type": "string", "description": "Search name/description/labels (list)."},
                    "gender": {"type": "string", "description": "e.g. female, male, neutral (list)."},
                    "locale": {"type": "string", "description": "e.g. en-GB, en, fr (list)."},
                    "limit": {"type": "integer", "description": "Max results for list (default 20, max 50)."},
                    "voice_id": {"type": "string", "description": "Voice to preview or set."},
                    "public_owner_id": {"type": "string", "description": "ElevenLabs library voices only."},
                    "text": {"type": "string", "description": "Line to speak in a preview (max 300 chars)."},
                    "reason": {"type": "string", "description": "Why you chose this voice (set). Kept with it."},
                    "speed": {"type": "number"},
                    "stability": {"type": "number"},
                    "similarity_boost": {"type": "number"},
                    "style": {"type": "number"},
                    "use_speaker_boost": {"type": "boolean"},
                    "instructions": {"type": "string", "description": "OpenAI gpt-4o-mini-tts tone description."},
                },
                "required": ["action"],
            },
        }

    async def execute(self, action: str = "", **params: Any) -> Dict[str, Any]:
        try:
            if action == "current":
                return await self._tool_current()
            if action == "providers":
                return {"success": True, "active": self._prefs()["provider"],
                        "providers": [p.info() for p in self.providers.values()]}
            if action == "list":
                return await self._tool_list(**params)
            if action == "preview":
                return await self._tool_preview(**params)
            if action == "set":
                return await self._tool_set(**params)
            return {"success": False, "error": f"Unknown action '{action}'. Use current, providers, list, preview or set."}
        except VoiceError as exc:
            return {"success": False, "error": str(exc)}

    async def _tool_current(self) -> Dict[str, Any]:
        status = self.status()
        result: Dict[str, Any] = {
            "success": True, "provider": status["provider"], "provider_label": status["provider_label"],
            "configured": status["configured"], "voice": self.current_voice(),
            "settings": status["settings"], "max_chars": status["max_chars"],
            "available_providers": [p["id"] for p in status["providers"] if p["available"]],
        }
        if not status["configured"]:
            result["note"] = "No usable voice yet. Check `providers`, then `list` and `set` one."
        elif result["voice"] and result["voice"].get("default"):
            result["note"] = "This is the provider's default voice; you can list and choose your own."
        return result

    async def _tool_list(self, provider: Optional[str] = None, search: str = "", gender: str = "",
                         locale: str = "", accent: str = "", limit: int = 20, source: str = "account",
                         **_: Any) -> Dict[str, Any]:
        limit = max(1, min(int(limit or 20), 50))
        data = await self.list_voices(provider=provider, search=search, gender=gender,
                                      locale=locale or accent, limit=limit, source=source)
        if source == "library":
            usage = await self.usage()
            if usage and usage.get("voice_slot_limit") is not None:
                data["voice_slots"] = {"used": usage["voice_slots_used"], "limit": usage["voice_slot_limit"]}
        data.pop("provider_label", None)
        return data

    async def _tool_preview(self, provider: Optional[str] = None, voice_id: str = "", text: str = "",
                            public_owner_id: str = "", **_: Any) -> Dict[str, Any]:
        data = await self.preview(provider=provider, voice_id=voice_id, text=text,
                                  public_owner_id=public_owner_id)
        data["note"] = "The user can play this in the tool card."
        return data

    async def _tool_set(self, provider: Optional[str] = None, voice_id: str = "", public_owner_id: str = "",
                        reason: str = "", stability: Any = None, similarity_boost: Any = None, style: Any = None,
                        speed: Any = None, use_speaker_boost: Any = None, instructions: Any = None,
                        **_: Any) -> Dict[str, Any]:
        return await self.set_voice(
            provider=provider, voice_id=voice_id, public_owner_id=public_owner_id, reason=reason,
            stability=stability, similarity_boost=similarity_boost, style=style, speed=speed,
            use_speaker_boost=use_speaker_boost, instructions=instructions,
        )
