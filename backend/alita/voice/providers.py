"""
Text-to-speech providers.

Each provider turns text into MP3 audio in a named voice and can list the
voices it offers:

* :class:`EdgeTTSProvider` — Microsoft Edge's online neural voices. Free, no
  API key, 300+ natural voices. Default when available.
* :class:`ElevenLabsProvider` — premium quality, per-character credits.
* :class:`OpenAITTSProvider` — cheap, uses the OpenAI key; supports tone
  instructions with ``gpt-4o-mini-tts``.
* :class:`BrowserProvider` — no server audio: the UI speaks with the
  operating system's voices (Web Speech API), fully offline.

Providers are stateless apart from a cached voice list; :class:`VoiceService`
owns configuration and caching.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from ..core import config
from .text import rate_percent

logger = logging.getLogger(__name__)

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"
ELEVENLABS_MODEL = "eleven_multilingual_v2"
ELEVENLABS_MODELS = [
    ("eleven_multilingual_v2", "Multilingual v2: natural and consistent (1 credit per character)"),
    ("eleven_flash_v2_5", "Flash v2.5: fastest, about half the credits"),
    ("eleven_turbo_v2_5", "Turbo v2.5: fast, about half the credits"),
    ("eleven_v3", "v3: most expressive"),
]
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_TTS_MODELS = ["gpt-4o-mini-tts", "tts-1", "tts-1-hd"]
VOICE_LIST_TTL = 3600


class VoiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class VoiceInfo:
    id: str
    name: str
    provider: str
    gender: Optional[str] = None
    age: Optional[str] = None
    accent: Optional[str] = None
    locale: Optional[str] = None
    locale_name: Optional[str] = None
    description: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    preview_url: Optional[str] = None
    owner_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "voice_id": self.id, "name": self.name, "provider": self.provider,
            "gender": self.gender, "locale": self.locale, "locale_name": self.locale_name,
            "description": self.description, "tags": self.tags, "preview_url": self.preview_url,
        }
        if self.age:
            d["age"] = self.age
        if self.accent:
            d["accent"] = self.accent
        if self.owner_id:
            d["public_owner_id"] = self.owner_id
        d.update(self.extra)
        return d


class TTSProvider:
    id: str = ""
    label: str = ""
    description: str = ""
    free: bool = False
    requires_key: bool = False
    supports_preview: bool = False  # server-side audio previews
    max_chars: int = 4000
    default_voice_id: Optional[str] = None
    default_voice_name: Optional[str] = None

    def available(self) -> bool:
        return True

    def availability_error(self) -> Optional[str]:
        return None

    def models(self) -> List[str]:
        return []

    async def list_voices(self, search: str = "", locale: str = "", gender: str = "",
                          limit: int = 100, source: str = "account") -> List[VoiceInfo]:
        return []

    def synth(self, text: str, voice_id: str, settings: Dict[str, Any]) -> AsyncIterator[bytes]:
        raise NotImplementedError

    async def stock_preview(self, voice_id: str) -> Optional[str]:
        """A free sample URL for a voice, when the provider offers one."""
        return None

    async def resolve_voice(self, voice_id: str, public_owner_id: str = "") -> Dict[str, Any]:
        """Validate/import a voice and return {'voice_id', 'name'}."""
        return {"voice_id": voice_id, "name": voice_id}

    def info(self) -> Dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "description": self.description, "free": self.free,
            "requires_key": self.requires_key, "available": self.available(),
            "error": self.availability_error(), "models": self.models(),
            "supports_preview": self.supports_preview, "max_chars": self.max_chars,
        }


# ─── Edge (free) ─────────────────────────────────────────────────────

def _edge_available() -> bool:
    return importlib.util.find_spec("edge_tts") is not None


def _edge_name(short_name: str, friendly: str) -> str:
    """'en-GB-SoniaNeural' / 'Microsoft Sonia Online (Natural) - English (United Kingdom)' -> 'Sonia'."""
    m = re.search(r"Microsoft (?P<name>.+?) Online", friendly or "")
    if m:
        name = m.group("name")
    else:
        parts = (short_name or "").split("-")
        name = parts[2] if len(parts) > 2 else short_name
    name = re.sub(r"(Multilingual)?Neural$", "", name)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return name.strip() or short_name


class EdgeTTSProvider(TTSProvider):
    id = "edge"
    label = "Edge neural voices"
    description = "Free, no key needed. Microsoft's natural neural voices (300+ languages)."
    free = True
    max_chars = 5000
    default_voice_id = "en-GB-SoniaNeural"
    default_voice_name = "Sonia"
    _cache: Optional[List[Dict[str, Any]]] = None
    _cache_at: float = 0.0

    def available(self) -> bool:
        return _edge_available()

    def availability_error(self) -> Optional[str]:
        if _edge_available():
            return None
        return "The 'edge-tts' package is not installed (pip install edge-tts)."

    async def _raw_voices(self) -> List[Dict[str, Any]]:
        if self._cache is not None and time.time() - self._cache_at < VOICE_LIST_TTL:
            return self._cache
        import edge_tts

        try:
            voices = await asyncio.wait_for(edge_tts.list_voices(), timeout=30)
        except Exception as exc:  # noqa: BLE001
            if self._cache:
                logger.warning("edge voice list refresh failed, using cache: %s", exc)
                return self._cache
            raise VoiceError(f"Couldn't load the Edge voice list: {exc}", 502) from exc
        EdgeTTSProvider._cache = voices
        EdgeTTSProvider._cache_at = time.time()
        return voices

    async def list_voices(self, search: str = "", locale: str = "", gender: str = "",
                          limit: int = 100, source: str = "account") -> List[VoiceInfo]:
        voices = await self._raw_voices()
        needle = (search or "").strip().lower()
        locale_prefix = (locale or "").strip().lower()
        gender_l = (gender or "").strip().lower()
        out: List[VoiceInfo] = []
        for v in voices:
            short = v.get("ShortName", "")
            friendly = v.get("FriendlyName", "")
            name = _edge_name(short, friendly)
            loc = v.get("Locale", "")
            loc_name = friendly.split(" - ", 1)[1] if " - " in friendly else loc
            if locale_prefix and not loc.lower().startswith(locale_prefix):
                continue
            if gender_l and (v.get("Gender") or "").lower() != gender_l:
                continue
            if needle and needle not in f"{name} {short} {friendly}".lower():
                continue
            tags = ["Natural"]
            if "Multilingual" in short:
                tags.append("Multilingual")
            out.append(VoiceInfo(
                id=short, name=name, provider=self.id, gender=v.get("Gender"), locale=loc,
                locale_name=loc_name, description=friendly, tags=tags,
            ))
            if len(out) >= max(1, min(int(limit or 100), 400)):
                break
        # English first, then other locales, both alphabetical.
        out.sort(key=lambda x: (not (x.locale or "").startswith("en"), x.locale or "", x.name))
        return out

    async def synth(self, text: str, voice_id: str, settings: Dict[str, Any]) -> AsyncIterator[bytes]:
        if not self.available():
            raise VoiceError(self.availability_error() or "Edge TTS unavailable", 503)
        import edge_tts

        rate = rate_percent(float(settings.get("speed", 1.0)))
        try:
            communicate = edge_tts.Communicate(text, voice_id, rate=rate)
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    yield chunk["data"]
        except VoiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VoiceError(f"Edge speech failed: {exc}", 502) from exc

    async def stock_preview(self, voice_id: str) -> Optional[str]:
        return None

    async def resolve_voice(self, voice_id: str, public_owner_id: str = "") -> Dict[str, Any]:
        try:
            voices = await self._raw_voices()
        except VoiceError:
            return {"voice_id": voice_id, "name": voice_id}  # offline: trust the id
        for v in voices:
            if v.get("ShortName") == voice_id:
                return {"voice_id": voice_id, "name": _edge_name(voice_id, v.get("FriendlyName", ""))}
        raise VoiceError(f"Unknown Edge voice '{voice_id}'. List the voices to see valid ids.", 404)


# ─── ElevenLabs (premium) ────────────────────────────────────────────

class ElevenLabsProvider(TTSProvider):
    id = "elevenlabs"
    label = "ElevenLabs"
    description = "Best quality and voice cloning. Paid: about 1 credit per character."
    requires_key = True
    supports_preview = True
    max_chars = 2500

    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None):
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None
        self._client_base: Optional[str] = None

    # configuration (read live so .env edits apply without a restart)
    @property
    def api_key(self) -> str:
        return os.getenv("ELEVENLABS_API_KEY", "").strip()

    def available(self) -> bool:
        return bool(self.api_key)

    def availability_error(self) -> Optional[str]:
        if self.api_key:
            return None
        return "No ElevenLabs API key. Add one in Settings → Voice (or set ELEVENLABS_API_KEY)."

    @property
    def model(self) -> str:
        return os.getenv("ELEVENLABS_MODEL", "").strip() or ELEVENLABS_MODEL

    def models(self) -> List[str]:
        ids = [m for m, _ in ELEVENLABS_MODELS]
        return ids if self.model in ids else [self.model, *ids]

    @property
    def max_chars(self) -> int:  # type: ignore[override]
        return max(100, min(10_000, config.env_int("ELEVENLABS_MAX_CHARS", 2500)))

    @property
    def base_url(self) -> str:
        return (os.getenv("ELEVENLABS_BASE_URL", "").strip() or ELEVENLABS_BASE_URL).rstrip("/")

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client_base != self.base_url:
            if self._client is not None:
                asyncio.ensure_future(self._client.aclose())
            self._client_base = self.base_url
            self._client = httpx.AsyncClient(
                base_url=self._client_base, transport=self._transport,
                timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=False,
            )
        return self._client

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise VoiceError(self.availability_error() or "ElevenLabs isn't configured", 503)
        return {"xi-api-key": self.api_key}

    @staticmethod
    def _error_message(resp: httpx.Response) -> str:
        detail: Any = None
        try:
            detail = resp.json().get("detail")
        except (ValueError, AttributeError):
            pass
        if isinstance(detail, dict):
            text = detail.get("message") or detail.get("status") or json.dumps(detail)[:300]
        elif isinstance(detail, list) and detail:
            text = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)[:300]
        elif isinstance(detail, str):
            text = detail
        else:
            text = (resp.text or "").strip()[:300] or resp.reason_phrase
        if resp.status_code == 401:
            return f"ElevenLabs rejected the request (401): {text}"
        return f"ElevenLabs error {resp.status_code}: {text}"

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._http().request(method, path, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise VoiceError(f"Couldn't reach ElevenLabs: {exc}", 502)
        if resp.status_code >= 400:
            message = self._error_message(resp)
            missing = resp.status_code == 404 or (resp.status_code == 400 and "not found" in message.lower())
            raise VoiceError(message, 404 if missing else 502)
        return resp.json()

    async def usage(self) -> Optional[Dict[str, Any]]:
        try:
            s = await self._json("GET", "/v1/user/subscription")
        except VoiceError:
            return None
        return {"characters_used": s.get("character_count"), "character_limit": s.get("character_limit"),
                "tier": s.get("tier"), "voice_slots_used": s.get("voice_slots_used"),
                "voice_slot_limit": s.get("voice_limit"), "resets_at_unix": s.get("next_character_count_reset_unix")}

    async def get_voice(self, voice_id: str) -> Dict[str, Any]:
        if not re.match(r"^[A-Za-z0-9]{1,64}$", voice_id or ""):
            raise VoiceError("voice_id looks invalid")
        return await self._json("GET", f"/v1/voices/{voice_id}")

    async def list_voices(self, search: str = "", locale: str = "", gender: str = "",
                          limit: int = 100, source: str = "account") -> List[VoiceInfo]:
        limit = max(1, min(int(limit or 100), 100))
        if source == "library":
            params = {k: v for k, v in {"search": search, "gender": gender, "accent": locale}.items() if v}
            data = await self._json("GET", "/v1/shared-voices", params={**params, "page_size": limit})
            return [VoiceInfo(
                id=v.get("voice_id", ""), name=v.get("name", ""), provider=self.id,
                gender=v.get("gender"), age=v.get("age"), accent=v.get("accent"),
                locale=v.get("language"), description=(v.get("description") or "")[:240] or None,
                tags=[t for t in [v.get("use_case"), v.get("descriptive")] if t],
                preview_url=v.get("preview_url"), owner_id=v.get("public_owner_id"),
                extra={"already_added": bool(v.get("is_added_by_user"))},
            ) for v in data.get("voices", [])][:limit]
        params = {k: v for k, v in {"search": search, "gender": gender}.items() if v}
        data = await self._json("GET", "/v2/voices", params={**params, "page_size": limit})
        out = []
        for v in data.get("voices", []):
            labels = v.get("labels") or {}
            out.append(VoiceInfo(
                id=v.get("voice_id", ""), name=v.get("name", ""), provider=self.id,
                gender=labels.get("gender"), age=labels.get("age"), accent=labels.get("accent"),
                description=(v.get("description") or "")[:240] or None,
                tags=[t for t in [labels.get("use_case"), labels.get("descriptive") if isinstance(labels.get("descriptive"), str) else None] if t],
                preview_url=v.get("preview_url"), extra={"category": v.get("category")},
            ))
        return out[:limit]

    async def stock_preview(self, voice_id: str) -> Optional[str]:
        info = await self.get_voice(voice_id)
        return info.get("preview_url") or None

    async def library_voice(self, voice_id: str, owner_id: str) -> Optional[Dict[str, Any]]:
        """Look up a public library voice (not yet added to the account)."""
        if not re.match(r"^[A-Za-z0-9]{1,128}$", owner_id or ""):
            raise VoiceError("public_owner_id looks invalid")
        data = await self._json("GET", "/v1/shared-voices", params={"owner_id": owner_id, "page_size": 100})
        return next((v for v in data.get("voices", []) if v.get("voice_id") == voice_id), None)

    async def resolve_voice(self, voice_id: str, public_owner_id: str = "") -> Dict[str, Any]:
        if not public_owner_id:
            info = await self.get_voice(voice_id)
            return {"voice_id": voice_id, "name": info.get("name") or voice_id}
        if not re.match(r"^[A-Za-z0-9]{1,128}$", public_owner_id):
            raise VoiceError("public_owner_id looks invalid")
        try:
            info = await self.get_voice(voice_id)
            return {"voice_id": voice_id, "name": info.get("name") or voice_id}
        except VoiceError as exc:
            if exc.status != 404:
                raise
        data = await self._json("GET", "/v1/shared-voices", params={"owner_id": public_owner_id, "page_size": 100})
        lib = next((v for v in data.get("voices", []) if v.get("voice_id") == voice_id), None)
        if not lib:
            raise VoiceError("That library voice wasn't found for this owner.")
        res = await self._json("POST", f"/v1/voices/add/{public_owner_id}/{voice_id}",
                               json={"new_name": (lib.get("name") or "Alita voice")[:100]})
        new_id = res.get("voice_id") or voice_id
        info = await self.get_voice(new_id)
        return {"voice_id": new_id, "name": info.get("name") or lib.get("name") or new_id}

    async def synth(self, text: str, voice_id: str, settings: Dict[str, Any]) -> AsyncIterator[bytes]:
        body = {"text": text, "model_id": self.model,
                "voice_settings": {k: settings[k] for k in ("stability", "similarity_boost", "style", "speed", "use_speaker_boost")
                                   if k in settings}}
        async with self._http().stream(
            "POST", f"/v1/text-to-speech/{voice_id}/stream", params={"output_format": "mp3_44100_128"},
            headers=self._headers(), json=body,
        ) as resp:
            if resp.status_code != 200:
                await resp.aread()
                raise VoiceError(self._error_message(resp), 502)
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ─── OpenAI (cheap) ──────────────────────────────────────────────────

_OPENAI_VOICES = [
    ("nova", "Nova", "female", "Warm, bright and natural"),
    ("coral", "Coral", "female", "Friendly and upbeat"),
    ("sage", "Sage", "female", "Calm and clear"),
    ("shimmer", "Shimmer", "female", "Soft and expressive"),
    ("alloy", "Alloy", "neutral", "Balanced and neutral"),
    ("ash", "Ash", "male", "Measured and confident"),
    ("echo", "Echo", "male", "Steady and clear"),
    ("onyx", "Onyx", "male", "Deep and authoritative"),
    ("fable", "Fable", "neutral", "Storytelling, expressive"),
    ("ballad", "Ballad", "male", "Smooth and lyrical"),
    ("verse", "Verse", "male", "Versatile and animated"),
]


class OpenAITTSProvider(TTSProvider):
    id = "openai"
    label = "OpenAI TTS"
    description = "Cheap and reliable, uses your OpenAI key. Tone can be described in words."
    requires_key = True
    max_chars = 4000
    default_voice_id = "nova"
    default_voice_name = "Nova"

    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None):
        self._transport = transport

    @property
    def api_key(self) -> str:
        return os.getenv("OPENAI_API_KEY", "").strip()

    def available(self) -> bool:
        return bool(self.api_key)

    def availability_error(self) -> Optional[str]:
        return None if self.api_key else "No OpenAI API key. Add one in Settings → Model (or set OPENAI_API_KEY)."

    @property
    def model(self) -> str:
        return os.getenv("OPENAI_TTS_MODEL", "").strip() or OPENAI_TTS_MODEL

    def models(self) -> List[str]:
        return [self.model, *[m for m in OPENAI_TTS_MODELS if m != self.model]]

    @property
    def base_url(self) -> str:
        return (os.getenv("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1").rstrip("/")

    async def list_voices(self, search: str = "", locale: str = "", gender: str = "",
                          limit: int = 100, source: str = "account") -> List[VoiceInfo]:
        needle = (search or "").strip().lower()
        out = []
        for vid, name, gen, desc in _OPENAI_VOICES:
            if gender and gen != gender.strip().lower():
                continue
            if needle and needle not in f"{vid} {name} {desc}".lower():
                continue
            out.append(VoiceInfo(id=vid, name=name, provider=self.id, gender=gen, locale="en",
                                 description=desc, tags=["Multilingual"]))
        return out[:limit]

    async def synth(self, text: str, voice_id: str, settings: Dict[str, Any]) -> AsyncIterator[bytes]:
        body: Dict[str, Any] = {
            "model": self.model, "voice": voice_id, "input": text,
            "response_format": "mp3", "speed": round(float(settings.get("speed", 1.0)), 2),
        }
        instructions = (settings.get("instructions") or "").strip()
        if instructions and self.model.startswith("gpt-4o"):
            body["instructions"] = instructions[:600]
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=120.0), transport=self._transport) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/audio/speech",
                    headers={"Authorization": f"Bearer {self.api_key}"}, json=body,
                ) as resp:
                    if resp.status_code != 200:
                        raw = await resp.aread()
                        msg = raw.decode("utf-8", errors="replace")[:300]
                        raise VoiceError(f"OpenAI speech error {resp.status_code}: {msg}", 502)
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            yield chunk
        except httpx.HTTPError as exc:
            raise VoiceError(f"Couldn't reach OpenAI: {exc}", 502) from exc


# ─── Browser (offline) ───────────────────────────────────────────────

class BrowserProvider(TTSProvider):
    id = "browser"
    label = "Browser voices"
    description = "Offline and free. Uses your device's installed system voices; quality varies."
    free = True
    max_chars = 5000
    supports_preview = False
    default_voice_id = None

    async def list_voices(self, search: str = "", locale: str = "", gender: str = "",
                          limit: int = 100, source: str = "account") -> List[VoiceInfo]:
        return []  # enumerated by the browser (Web Speech API)

    async def synth(self, text: str, voice_id: str, settings: Dict[str, Any]) -> AsyncIterator[bytes]:
        raise VoiceError("Browser voices are spoken locally by the UI, not the server.")
        yield b""  # pragma: no cover


def build_providers(transport: Optional[httpx.AsyncBaseTransport] = None) -> Dict[str, TTSProvider]:
    providers: List[TTSProvider] = [
        EdgeTTSProvider(),
        BrowserProvider(),
        ElevenLabsProvider(transport=transport),
        OpenAITTSProvider(transport=transport),
    ]
    return {p.id: p for p in providers}
