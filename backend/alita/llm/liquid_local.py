"""
Liquid AI local provider.

Runs a Liquid Foundation Model completely on-device. The model is a GGUF file
managed by Alita: the first time it is needed it is downloaded from Hugging
Face into ``~/.alita/models/liquid`` and every later run is fully offline.
Inference is done through ``llama-cpp-python`` (installed from
``requirements.txt``).

Configuration (environment / Settings → Model):

* ``LIQUID_LOCAL_MODEL`` — Hugging Face repo id (default
  ``LiquidAI/LFM2.5-1.2B-Instruct-GGUF``).
* ``LIQUID_LOCAL_FILE`` — GGUF filename inside the repo. Derived from the
  repo id and quantization when omitted.
* ``LIQUID_LOCAL_QUANT`` — quantization suffix (default ``Q4_K_M``).
* ``LIQUID_LOCAL_PATH`` — use an already-downloaded GGUF instead.
* ``LIQUID_LOCAL_CONTEXT`` — context window (default 8192).
* ``LIQUID_LOCAL_THREADS`` — CPU threads (default: half the cores).
* ``LIQUID_LOCAL_GPU_LAYERS`` — layers offloaded to the GPU (default 0).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from ..core import config
from .provider import LLMError, LLMProvider, clean_messages

logger = logging.getLogger(__name__)

DEFAULT_REPO = "LiquidAI/LFM2.5-1.2B-Instruct-GGUF"
DEFAULT_QUANT = "Q4_K_M"
DEFAULT_CONTEXT = 8192
DOWNLOAD_CHUNK = 1024 * 512

_MODEL_CHOICES = [
    "LiquidAI/LFM2.5-1.2B-Instruct-GGUF",
    "LiquidAI/LFM2.5-350M-GGUF",
    "LiquidAI/LFM2.5-230M-GGUF",
    "LiquidAI/LFM2.5-1.2B-Thinking-GGUF",
    "LiquidAI/LFM2.5-2.6B-GGUF",
]

_STATE_LOCK = threading.Lock()
_DOWNLOAD_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {
    "state": "idle",
    "progress": 0.0,
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "error": None,
    "started_at": None,
    "finished_at": None,
}

_ENGINE_LOCK = threading.Lock()
_ENGINE: Any = None
_ENGINE_KEY: Optional[str] = None
_RUNTIME_AVAILABLE: Optional[bool] = None
_RUNTIME_ERROR: Optional[str] = None


def _runtime() -> tuple[bool, Optional[str]]:
    global _RUNTIME_AVAILABLE, _RUNTIME_ERROR
    if _RUNTIME_AVAILABLE is None:
        try:
            import llama_cpp  # noqa: F401

            _RUNTIME_AVAILABLE = True
            _RUNTIME_ERROR = None
        except Exception as exc:  # noqa: BLE001
            _RUNTIME_AVAILABLE = False
            _RUNTIME_ERROR = (
                "llama-cpp-python isn't installed. Run "
                "`pip install -r requirements.txt` in the backend environment."
            )
            logger.debug("llama-cpp-python unavailable: %s", exc)
    return _RUNTIME_AVAILABLE, _RUNTIME_ERROR


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


def models_dir() -> Path:
    path = config.alita_home() / "models" / "liquid"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_repo(model: Optional[str] = None) -> str:
    value = (model or os.getenv("LIQUID_LOCAL_MODEL", "") or DEFAULT_REPO).strip()
    if value.startswith("hf.co/"):
        value = value[6:]
    return value.rstrip("/")


def resolve_file(repo: str, filename: Optional[str] = None) -> str:
    explicit = (filename or os.getenv("LIQUID_LOCAL_FILE", "") or "").strip()
    if explicit:
        return explicit
    quant = (os.getenv("LIQUID_LOCAL_QUANT", "") or DEFAULT_QUANT).strip()
    name = repo.rsplit("/", 1)[-1]
    for suffix in ("-GGUF", "-gguf"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return f"{name}-{quant}.gguf"


def resolve_quant(filename: str) -> str:
    stem = Path(filename).stem
    for quant in ("Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M", "Q4_0", "BF16", "F16"):
        if stem.endswith(quant):
            return quant
    return ""


def model_path(repo: str, filename: str) -> Path:
    override = (os.getenv("LIQUID_LOCAL_PATH", "") or "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return models_dir() / filename


def _set_state(**updates: Any) -> None:
    with _STATE_LOCK:
        _STATE.update(updates)


def _snapshot() -> Dict[str, Any]:
    with _STATE_LOCK:
        return dict(_STATE)


def is_downloaded(repo: Optional[str] = None, filename: Optional[str] = None) -> bool:
    repo = resolve_repo(repo)
    file = resolve_file(repo, filename)
    path = model_path(repo, file)
    return path.is_file() and path.stat().st_size > 0


def liquid_status(repo: Optional[str] = None, filename: Optional[str] = None) -> Dict[str, Any]:
    repo = resolve_repo(repo)
    file = resolve_file(repo, filename)
    path = model_path(repo, file)
    downloaded = path.is_file() and path.stat().st_size > 0
    runtime, runtime_error = _runtime()
    state = _snapshot()
    if downloaded:
        state["state"] = "ready"
        state["progress"] = 1.0
        state["error"] = None
    elif state["state"] == "downloading":
        pass
    elif state["state"] == "ready":
        state["state"] = "idle"
    return {
        "repo": repo,
        "file": file,
        "quant": resolve_quant(file),
        "path": str(path),
        "downloaded": downloaded,
        "size_bytes": path.stat().st_size if downloaded else 0,
        "runtime": "llama_cpp" if runtime else None,
        "runtime_available": runtime,
        "runtime_error": runtime_error,
        "context": _env_int("LIQUID_LOCAL_CONTEXT", DEFAULT_CONTEXT),
        "choices": _MODEL_CHOICES,
        **state,
    }


def _download(repo: str, filename: str, target: Path) -> Path:
    url = f"https://huggingface.co/{repo}/resolve/main/{filename}?download=true"
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    _set_state(state="downloading", progress=0.0, downloaded_bytes=0, total_bytes=0,
               error=None, started_at=time.time(), finished_at=None)
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(None, connect=30.0)) as resp:
            if resp.status_code >= 400:
                raise LLMError(
                    f"Could not download the Liquid model ({resp.status_code}). "
                    "Check the repo and filename, or download it manually and set LIQUID_LOCAL_PATH."
                )
            if "text/html" in (resp.headers.get("content-type") or ""):
                raise LLMError(
                    "Hugging Face returned a web page instead of a model file. "
                    "Check the repo and filename, or set LIQUID_LOCAL_PATH."
                )
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            last_reported = 0.0
            with part.open("wb") as fh:
                for chunk in resp.iter_bytes(DOWNLOAD_CHUNK):
                    if chunk:
                        fh.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if now - last_reported > 0.25:
                            last_reported = now
                            _set_state(
                                downloaded_bytes=done,
                                total_bytes=total,
                                progress=(done / total) if total else 0.0,
                            )
        if part.stat().st_size <= 0:
            raise LLMError("The downloaded Liquid model file is empty.")
        os.replace(part, target)
        _set_state(state="ready", progress=1.0, downloaded_bytes=target.stat().st_size,
                   total_bytes=target.stat().st_size, finished_at=time.time())
        return target
    except LLMError:
        part.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        part.unlink(missing_ok=True)
        raise LLMError(
            "Could not download the Liquid model. Connect to the internet once to fetch it, "
            f"then it works offline. ({exc})"
        ) from exc


def prepare(repo: Optional[str] = None, filename: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    """Download the configured GGUF if needed. Safe to call repeatedly."""
    repo = resolve_repo(repo)
    file = resolve_file(repo, filename)
    target = model_path(repo, file)
    if target.is_file() and target.stat().st_size > 0 and not force:
        _set_state(state="ready", progress=1.0, error=None)
        return liquid_status(repo, file)
    with _DOWNLOAD_LOCK:
        if target.is_file() and target.stat().st_size > 0 and not force:
            _set_state(state="ready", progress=1.0, error=None)
            return liquid_status(repo, file)
        with _STATE_LOCK:
            if _STATE["state"] == "downloading" and not force:
                return liquid_status(repo, file)
            _STATE.update(state="downloading", progress=0.0, downloaded_bytes=0,
                          total_bytes=0, error=None, started_at=time.time(), finished_at=None)
        try:
            _download(repo, file, target)
        except Exception as exc:  # noqa: BLE001
            _set_state(state="error", error=str(exc), finished_at=time.time())
            raise
    return liquid_status(repo, file)


def start_prepare(repo: Optional[str] = None, filename: Optional[str] = None) -> Dict[str, Any]:
    """Kick off a background download and return immediately."""
    repo = resolve_repo(repo)
    file = resolve_file(repo, filename)
    with _STATE_LOCK:
        busy = _STATE["state"] == "downloading"
    if busy:
        return liquid_status(repo, file)

    def worker() -> None:
        try:
            prepare(repo, file)
        except Exception:  # noqa: BLE001
            logger.warning("Liquid model download failed", exc_info=True)

    threading.Thread(target=worker, name="liquid-model-download", daemon=True).start()
    return liquid_status(repo, file)


def _pick_engine(path: Path, context: int, threads: int, gpu_layers: int) -> Any:
    global _ENGINE, _ENGINE_KEY
    key = f"{path}|{context}|{threads}|{gpu_layers}"
    with _ENGINE_LOCK:
        if _ENGINE is not None and _ENGINE_KEY == key:
            return _ENGINE
        try:
            from llama_cpp import Llama
        except Exception as exc:  # noqa: BLE001
            raise LLMError(
                "The offline Liquid model needs llama-cpp-python. Install the backend "
                "requirements (`pip install -r requirements.txt`) and try again."
            ) from exc
        try:
            _ENGINE = Llama(
                model_path=str(path),
                n_ctx=context,
                n_threads=threads,
                n_gpu_layers=gpu_layers,
                verbose=False,
                logits_all=False,
            )
            _ENGINE_KEY = key
        except Exception as exc:  # noqa: BLE001
            _ENGINE = None
            _ENGINE_KEY = None
            raise LLMError(f"Could not load the Liquid model: {exc}") from exc
        return _ENGINE


def _flatten_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    texts: List[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            texts.append(str(part.get("text", "")))
        elif isinstance(part, str):
            texts.append(part)
    return "\n".join(texts)


def _convert_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for message in clean_messages(messages):
        msg = dict(message)
        msg["content"] = _flatten_content(msg.get("content"))
        msg.pop("reasoning_content", None)
        msg.pop("reasoning", None)
        out.append(msg)
    return out


class LiquidLocalProvider(LLMProvider):
    provider_id = "liquid_local"

    def __init__(self, model: str = "", base_url: str = "", **_: Any):
        self.model = resolve_repo(model)
        self.base_url = base_url or ""
        self.context = _env_int("LIQUID_LOCAL_CONTEXT", DEFAULT_CONTEXT)
        self.threads = _env_int("LIQUID_LOCAL_THREADS", max(1, (os.cpu_count() or 4) // 2))
        self.gpu_layers = _env_int("LIQUID_LOCAL_GPU_LAYERS", 0)
        self.max_tokens = _env_int("LIQUID_LOCAL_MAX_TOKENS", 4096)

    @property
    def context_window(self) -> int:
        override = int(os.getenv("ALITA_CONTEXT_WINDOW", "0") or 0)
        return override or self.context

    def status(self) -> Dict[str, Any]:
        return liquid_status(self.model)

    def ensure_ready(self) -> Path:
        repo = self.model
        filename = resolve_file(repo)
        path = model_path(repo, filename)
        if not (path.is_file() and path.stat().st_size > 0):
            path = Path(prepare(repo, filename)["path"])
        return path

    async def chat_stream(self, messages, tools=None, temperature=None, max_tokens=None):
        try:
            path = await asyncio.to_thread(self.ensure_ready)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Liquid offline model isn't ready: {exc}") from exc
        engine = await asyncio.to_thread(
            _pick_engine, path, self.context, self.threads, self.gpu_layers
        )
        payload_messages = _convert_messages(messages)
        limit = min(int(max_tokens or self.max_tokens), self.max_tokens)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def worker() -> None:
            try:
                stream = engine.create_chat_completion(
                    messages=payload_messages,
                    tools=tools or None,
                    temperature=0.1 if temperature is None else temperature,
                    max_tokens=limit,
                    stream=True,
                )
                for chunk in stream:
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))

        threading.Thread(target=worker, name="liquid-local-generation", daemon=True).start()

        partial: Dict[int, Dict[str, Any]] = {}
        emitted = False
        usage: Dict[str, int] = {}
        while True:
            kind, payload = await queue.get()
            if kind == "done":
                break
            if kind == "error":
                raise LLMError(f"Liquid offline model error: {payload}") from payload
            chunk = payload or {}
            if chunk.get("usage"):
                raw = chunk["usage"] or {}
                usage = {
                    "prompt_tokens": raw.get("prompt_tokens", 0),
                    "completion_tokens": raw.get("completion_tokens", 0),
                    "cached_tokens": 0,
                }
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    emitted = True
                    yield {"content": delta["content"]}
                for tool_call in delta.get("tool_calls") or []:
                    index = tool_call.get("index", 0)
                    slot = partial.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tool_call.get("id"):
                        slot["id"] = tool_call["id"]
                    function = tool_call.get("function") or {}
                    if function.get("name"):
                        slot["function"]["name"] += function["name"]
                    if function.get("arguments") is not None:
                        args = function["arguments"]
                        slot["function"]["arguments"] += args if isinstance(args, str) else json.dumps(args)
                    emitted = True
        if partial:
            calls = []
            for index in sorted(partial):
                call = partial[index]
                if not call["function"]["name"]:
                    continue
                if not call["id"]:
                    call["id"] = f"call_liquid_{index}_{int(time.time() * 1000)}"
                calls.append(call)
            if calls:
                yield {"tool_calls": calls}
        if usage:
            yield {"usage": usage}
        elif not emitted:
            yield {"content": ""}

    async def aclose(self) -> None:
        return None
