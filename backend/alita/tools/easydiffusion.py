"""
EasyDiffusion REST Client — Local Image Generation Integration.

Interfaces with a locally running EasyDiffusion 3.x instance over its REST API
(default endpoint: http://localhost:9000).

Every successful ``txt2img`` / ``img2img`` call **automatically saves** the
generated image to the ``outputs/`` directory in the application root (or
``ALITA_OUTPUT_DIR`` if set). A JSON sidecar ``<filename>.json`` is written
alongside each image, capturing the full render metadata (prompt, negative
prompt, seed, sampler, steps, cfg_scale, dimensions, task_id, timestamp).
The gallery view reads these files to present the user's image history.

Endpoints actually exposed by EasyDiffusion 3.x (verified by probing the live
server and reading ``media/js/task-manager.js`` / ``media/js/main.js``):

  - POST /render                  → txt2img / img2img
       Response (202 Rendering, JSON):
           {"status": "Rendering",
            "queue": <int>,
            "stream": "/image/stream/<task>",
            "task":   <int>}                  ← note the key is ``task``, not ``task_id``

  - GET  /image/stream/<task>     → NDJSON stream (NOT true SSE; the response
                                    body is a concatenation of JSON objects
                                    appended as the render progresses).
       Records emitted:
           {"step": N, "step_time": <sec>, "total_steps": M}
           ...
           {"status": "succeeded"|"failed", "render_request": {...},
            "task_data": {...}, "output": [{"data": "...", "path": "...", "seed": N}]}
       On failure the record carries a ``detail`` field with the error text.

  - GET  /get/models              → {"models": [{"model": "...", "name": "...", "tags": [...]}, ...]}
  - GET  /ping                    → service status
"""

import asyncio
import base64
import json
import os
import re
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import httpx
    HAS_HTTPX = True
except ImportError:  # pragma: no cover - httpx is a declared dependency
    HAS_HTTPX = False


#: Hard cap on synchronous waits. A 512x512 image renders in ~5-30s on a
#: consumer GPU; 240s is a generous ceiling that still bounds the agent loop.
DEFAULT_RENDER_TIMEOUT = 240

#: How long to wait between stream reads when polling progress.
POLL_INTERVAL = 0.5


class EasyDiffusionTool:
    """
    Client for the EasyDiffusion REST API.

    Generates images from text prompts (txt2img) and image prompts (img2img),
    polls the NDJSON progress stream until completion, and returns base-64 image
    data plus server-relative paths. Every successful render is **automatically
    saved** to ``self.outputs_dir`` (default ``<app_root>/outputs/``) with a
    timestamped filename and a JSON metadata sidecar. Construct one instance
    per agent.
    """

    def __init__(self, base_url: str = "http://localhost:9000",
                 render_timeout: int = DEFAULT_RENDER_TIMEOUT,
                 outputs_dir: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.render_timeout = render_timeout
        self._client: Optional[httpx.AsyncClient] = None

        # Resolve the outputs directory:
        #   1. Explicit constructor arg
        #   2. ALITA_OUTPUT_DIR env var
        #   3. <app_root>/outputs/ (two levels up from this file)
        if outputs_dir:
            self.outputs_dir = Path(outputs_dir)
        elif os.getenv("ALITA_OUTPUT_DIR"):
            self.outputs_dir = Path(os.getenv("ALITA_OUTPUT_DIR"))
        else:
            # backend/alita/tools/easydiffusion.py → ../../../outputs/
            self.outputs_dir = Path(__file__).resolve().parent.parent.parent.parent / "outputs"
        self.outputs_dir.mkdir(parents=True, exist_ok=True)

    @property
    def definition(self) -> Dict[str, Any]:
        return {
            "name": "easy_diffusion",
            "description": (
                "Generate images using a locally running EasyDiffusion instance. "
                "Supports text-to-image (txt2img), image-to-image (img2img), "
                "listing installed models, and monitoring generation progress. "
                "txt2img/img2img return the final images directly (synchronous)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["txt2img", "img2img", "status", "models"],
                        "description": "The image generation action.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The positive text prompt describing what to generate.",
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "Things to avoid in the generated image.",
                        "default": "ugly, blurry, low quality, distorted, deformed",
                    },
                    "model": {
                        "type": "string",
                        "description": "The stable diffusion model to use. Use action='models' to list available models. Common values: 'sd-v1-5', 'flux1-dev-Q4_0', 'Juggernaut_RunDiffusionPhoto2_Lightning_4Steps'. Leave empty to use the server's default model.",
                        "default": "",
                    },
                    "width": {
                        "type": "integer",
                        "description": "Image width in pixels.",
                        "default": 512,
                    },
                    "height": {
                        "type": "integer",
                        "description": "Image height in pixels.",
                        "default": 512,
                    },
                    "steps": {
                        "type": "integer",
                        "description": "Number of inference steps.",
                        "default": 25,
                    },
                    "seed": {
                        "type": "integer",
                        "description": "Random seed for reproducibility. -1 for random.",
                        "default": -1,
                    },
                    "sampler": {
                        "type": "string",
                        "description": "Sampling method (e.g., euler_a, dpmpp_2m, lms).",
                        "default": "euler_a",
                    },
                    "cfg_scale": {
                        "type": "number",
                        "description": "Classifier-free guidance scale.",
                        "default": 7.5,
                    },
                    "num_images": {
                        "type": "integer",
                        "description": "Number of images to generate (txt2img only).",
                        "default": 1,
                    },
                    "init_image": {
                        "type": "string",
                        "description": "Base64-encoded initial image for img2img.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Task ID to check status for (action=status only).",
                    },
                    "wait": {
                        "type": "boolean",
                        "description": (
                            "If True (default), txt2img/img2img poll the progress stream "
                            "and return final images. If False, return immediately with "
                            "the task_id so the caller can poll via action=status."
                        ),
                        "default": True,
                    },
                },
                "required": ["action"],
            },
        }

    def _get_client(self) -> "httpx.AsyncClient":
        if self._client is None and HAS_HTTPX:
            # Stream reads need no timeout on the read itself, but we cap the
            # overall wait via the poll loop. connect/read timeouts are 30s so
            # we surface unreachable servers promptly.
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(30.0, read=None),
            )
        return self._client

    async def execute(
        self,
        action: str,
        prompt: str = "",
        negative_prompt: str = "ugly, blurry, low quality, distorted, deformed",
        model: str = "",
        width: int = 512,
        height: int = 512,
        steps: int = 25,
        seed: int = -1,
        sampler: str = "euler_a",
        cfg_scale: float = 7.5,
        num_images: int = 1,
        init_image: str = "",
        task_id: str = "",
        wait: bool = True,
    ) -> Dict[str, Any]:
        """Execute an EasyDiffusion action."""
        if not HAS_HTTPX:
            return {"success": False, "error": "httpx not installed. Run: pip install httpx"}

        try:
            if action == "txt2img":
                if not prompt:
                    return {"success": False, "error": "prompt is required for txt2img"}
                return await self._txt2img(
                    prompt, negative_prompt, width, height,
                    steps, seed, sampler, cfg_scale, num_images,
                    wait=wait, model=model,
                )
            elif action == "img2img":
                if not prompt:
                    return {"success": False, "error": "prompt is required for img2img"}
                if not init_image:
                    return {"success": False, "error": "init_image (base64) is required for img2img"}
                return await self._img2img(
                    prompt, negative_prompt, init_image, width, height,
                    steps, seed, sampler, cfg_scale,
                    wait=wait, model=model,
                )
            elif action == "status":
                return await self._check_status(task_id)
            elif action == "models":
                return await self._get_models()
            else:
                return {"success": False, "error": f"Unknown action: {action}"}
        except httpx.HTTPError as e:
            return {"success": False, "error": f"EasyDiffusion API error: {e}"}
        except Exception as e:
            # Don't let an internal error crash the agent loop — surface as a
            # tool failure so the LLM can adapt.
            return {"success": False, "error": str(e)}

    # ─── Submit / render ────────────────────────────────────────────

    async def _submit_render(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        POST /render and return the parsed response dict, raising on HTTP error.

        EasyDiffusion 3.x responds with:

            {"status":"Rendering","queue":N,"stream":"/image/stream/<task>","task":<int>}
        """
        client = self._get_client()
        response = await client.post("/render", json=payload)
        response.raise_for_status()
        return response.json()

    def _extract_task_id(self, data: Dict[str, Any]) -> str:
        """Read the task id regardless of which key EasyDiffusion used."""
        # 3.x exposes `task` as an int; older builds used `task_id` / `id`.
        for key in ("task", "task_id", "id"):
            if key in data and data[key] is not None:
                return str(data[key])
        return ""

    async def _txt2img(
        self,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        steps: int,
        seed: int,
        sampler: str,
        cfg_scale: float,
        num_images: int,
        wait: bool = True,
        model: str = "",
    ) -> Dict[str, Any]:
        if seed == -1:
            seed = random.randint(0, 2**31 - 1)

        payload = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "width": width,
            "height": height,
            "num_outputs": num_images,
            "num_inference_steps": steps,
            "guidance_scale": cfg_scale,
            "sampler_name": sampler,
            "seed": seed,
        }
        if model:
            payload["use_stable_diffusion_model"] = model
        data = await self._submit_render(payload)
        task_id = self._extract_task_id(data)
        ack = {
            "success": True,
            "task_id": task_id,
            "stream": data.get("stream", ""),
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "model": model or "default",
            "seed": seed,
            "sampler": sampler,
            "steps": steps,
            "cfg_scale": cfg_scale,
            "width": width,
            "height": height,
            "status": data.get("status", "processing"),
            "queue_position": data.get("queue"),
        }

        if not wait or not task_id:
            ack["message"] = "Generation submitted. Poll action=status with this task_id for progress."
            return ack

        # Synchronous: poll the NDJSON stream until completion.
        final = await self._await_completion(task_id, prompt=payload)
        final.update({
            "task_id": task_id,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "seed": seed,
            "sampler": sampler,
            "steps": steps,
            "cfg_scale": cfg_scale,
            "width": width,
            "height": height,
        })
        return final

    async def _img2img(
        self,
        prompt: str,
        negative_prompt: str,
        init_image_b64: str,
        width: int,
        height: int,
        steps: int,
        seed: int,
        sampler: str,
        cfg_scale: float,
        wait: bool = True,
        model: str = "",
    ) -> Dict[str, Any]:
        if seed == -1:
            seed = random.randint(0, 2**31 - 1)

        payload = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "init_image": init_image_b64,
            "width": width,
            "height": height,
            "num_outputs": 1,
            "num_inference_steps": steps,
            "guidance_scale": cfg_scale,
            "sampler_name": sampler,
            "seed": seed,
        }
        if model:
            payload["use_stable_diffusion_model"] = model
        data = await self._submit_render(payload)
        task_id = self._extract_task_id(data)
        ack = {
            "success": True,
            "task_id": task_id,
            "stream": data.get("stream", ""),
            "prompt": prompt,
            "model": model or "default",
            "seed": seed,
            "status": data.get("status", "processing"),
        }

        if not wait or not task_id:
            ack["message"] = "Generation submitted. Poll action=status with this task_id for progress."
            return ack

        final = await self._await_completion(task_id, prompt=payload)
        final.update({"task_id": task_id, "prompt": prompt, "seed": seed})
        return final

    # ─── Stream polling ──────────────────────────────────────────────

    async def _await_completion(self, task_id: str, *, prompt: Dict[str, Any]) -> Dict[str, Any]:
        """
        Poll ``GET /image/stream/<task>`` until a terminal status appears.

        Returns a final result dict. On success the ``images`` field is a list
        of ``{path, url, seed}`` records. On failure ``success`` is False and
        ``error`` carries the server-reported detail.

        Bounded by ``self.render_timeout`` via ``asyncio.wait_for``: if the
        render takes longer than the configured timeout, returns
        ``status=timeout``. This also handles the hung-stream case where the
        server never closes the connection.
        """
        try:
            result = await asyncio.wait_for(
                self._poll_stream(task_id, prompt),
                timeout=self.render_timeout,
            )
        except asyncio.TimeoutError:
            return {
                "success": False,
                "task_id": task_id,
                "status": "timeout",
                "error": f"Generation did not complete within {self.render_timeout}s",
            }
        return result

    async def _poll_stream(self, task_id: str, prompt: Dict[str, Any]) -> Dict[str, Any]:
        """
        Poll ``GET /image/stream/<task>`` until a terminal record arrives.

        EasyDiffusion's stream endpoint has *consume* semantics: each GET
        returns only the records produced since the last GET (an empty body
        when nothing has happened yet). We poll at ``POLL_INTERVAL`` seconds
        until a ``{"status": "succeeded" | "failed" | "error", ...}`` record
        appears.

        Bounded externally by ``asyncio.wait_for`` with ``self.render_timeout``:
        if the render takes longer than the configured timeout, the poller is
        cancelled and the caller surfaces a ``status=timeout`` result.
        """
        client = self._get_client()
        stream_path = f"/image/stream/{task_id}"
        last_step = -1
        last_total = -1
        last_status = "processing"
        terminal: Optional[Dict[str, Any]] = None

        while terminal is None:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                response = await client.get(stream_path)
                response.raise_for_status()
                body = response.text
            except httpx.HTTPError as e:
                return {
                    "success": False,
                    "task_id": task_id,
                    "status": "error",
                    "error": f"Could not poll /image/stream/{task_id}: {e}",
                }
            if not body:
                continue

            # Each GET returns records produced since the last GET — parse
            # this batch fresh. EasyDiffusion emits concatenated JSON objects
            # (no separators), so we walk the buffer tracking brace depth and
            # respecting string literals / backslash escapes.
            depth = 0
            in_string = False
            escape_next = False
            i = 0
            while i < len(body):
                c = body[i]
                if in_string:
                    if escape_next:
                        escape_next = False
                    elif c == "\\":
                        escape_next = True
                    elif c == '"':
                        in_string = False
                elif c == '"':
                    in_string = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        obj_text = body[: i + 1]
                        body = body[i + 1:]
                        i = -1
                        depth = 0
                        in_string = False
                        escape_next = False
                        record = self._safe_json(obj_text)
                        if isinstance(record, dict):
                            if "step" in record:
                                last_step = int(record.get("step", -1))
                                last_total = int(record.get("total_steps", -1))
                            last_status = record.get("status", last_status)
                            if record.get("status") in ("succeeded", "failed", "error"):
                                terminal = record
                                break
                i += 1

        if terminal is None:
            # Unreachable — the loop above only exits when terminal is set or
            # asyncio.wait_for cancels us (handled by the outer wrapper). Kept
            # for safety.
            return {
                "success": False,
                "task_id": task_id,
                "status": "interrupted",
                "error": f"Polling ended without a terminal record (last step {last_step}/{last_total}, status={last_status})",
                "last_step": last_step,
                "last_total_steps": last_total,
                "last_status": last_status,
            }

        if terminal.get("status") != "succeeded":
            detail = terminal.get("detail") or terminal.get("error") or "render failed"
            return {
                "success": False,
                "task_id": task_id,
                "status": terminal.get("status", "failed"),
                "error": str(detail),
                "detail": detail,
                "last_step": last_step,
                "last_total_steps": last_total,
            }

        raw_output = terminal.get("output") or []
        render_request = terminal.get("render_request") or prompt or {}
        images: List[Dict[str, Any]] = []
        for idx, entry in enumerate(raw_output):
            if not isinstance(entry, dict):
                continue
            data_uri = entry.get("data") or ""
            path = entry.get("path") or ""
            seed_val = entry.get("seed")
            url = ""

            # Always save the generated image to disk so the gallery can
            # display it later. Decode the base64 data URI (or download
            # from the EasyDiffusion path URL) and write a timestamped
            # file plus a JSON metadata sidecar.
            saved = self._save_image(
                data_uri=data_uri,
                remote_path=path,
                seed=seed_val,
                render_request=render_request,
                task_id=task_id,
                index=idx,
            )
            if saved:
                # The local /images/{filename}/file endpoint serves the
                # saved file — use that URL so the UI doesn't need to
                # proxy EasyDiffusion or embed megabytes of base64.
                url = f"/api/images/{saved['filename']}/file"
                path = saved["filepath"]

            if not url:
                if path:
                    cached = f"{path}?t={int(time.time())}"
                    url = cached if cached.startswith("http") else f"{self.base_url}{cached}"
                elif data_uri and (data_uri.startswith("http") or data_uri.startswith("data:")):
                    url = data_uri

            images.append({
                "path": path,
                "url": url,
                "seed": seed_val,
                "has_data": bool(data_uri),
                "data_preview": data_uri[:80] if data_uri else "",
                "saved_path": saved["filepath"] if saved else "",
                "filename": saved["filename"] if saved else "",
            })

        return {
            "success": True,
            "task_id": task_id,
            "status": "completed",
            "images": images,
            "image_count": len(images),
            "elapsed_steps": last_step,
            "total_steps": last_total,
            "render_request": render_request,
        }

    # ─── Save to disk ──────────────────────────────────────────────

    def _save_image(
        self,
        data_uri: str,
        remote_path: str,
        seed: Optional[int],
        render_request: Dict[str, Any],
        task_id: str,
        index: int,
    ) -> Optional[Dict[str, str]]:
        """
        Save a generated image to ``self.outputs_dir``.

        Decodes the base64 data URI returned by EasyDiffusion (the common
        case) or, if only a server path is available, downloads the image
        from the EasyDiffusion server. Always writes a JSON metadata
        sidecar alongside the image file.

        Returns ``{"filename": ..., "filepath": ...}`` on success, ``None``
        if the image could not be saved.
        """
        # Determine the image bytes and format.
        image_bytes: Optional[bytes] = None
        ext = "png"

        if data_uri and data_uri.startswith("data:"):
            # data:image/jpeg;base64,/9j/4AAQ...
            header, _, b64_data = data_uri.partition(",")
            mime_match = re.match(r"data:(image/\w+)", header)
            if mime_match:
                mime = mime_match.group(1)
                ext = mime.split("/")[-1].replace("jpeg", "jpg")
            try:
                image_bytes = base64.b64decode(b64_data)
            except Exception:
                image_bytes = None
        elif remote_path:
            # Download from the EasyDiffusion server (sync — called from
            # an async context but this is fast for a local server).
            ext = Path(remote_path).suffix.lstrip(".") or "png"
            try:
                full_url = remote_path if remote_path.startswith("http") else f"{self.base_url}{remote_path}"
                resp = httpx.get(full_url, timeout=30.0)
                if resp.status_code == 200:
                    image_bytes = resp.content
                else:
                    image_bytes = None
            except Exception:
                image_bytes = None

        if image_bytes is None:
            return None

        # Build a timestamped filename: alita_20260711_143052_a3b2.png
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_seed = f"_{abs(int(seed)) % 0xFFFF:04x}" if seed is not None else ""
        filename = f"alita_{ts}{safe_seed}.{ext}"
        filepath = self.outputs_dir / filename

        # Avoid collisions in the rare case of same-second generation.
        counter = 1
        while filepath.exists():
            filename = f"alita_{ts}{safe_seed}_{counter}.{ext}"
            filepath = self.outputs_dir / filename
            counter += 1

        try:
            filepath.write_bytes(image_bytes)
        except OSError:
            return None

        # Write a JSON metadata sidecar.
        metadata = {
            "filename": filename,
            "prompt": render_request.get("prompt", ""),
            "negative_prompt": render_request.get("negative_prompt", ""),
            "model": render_request.get("use_stable_diffusion_model", "default"),
            "seed": seed,
            "sampler": render_request.get("sampler_name", ""),
            "steps": render_request.get("num_inference_steps", 0),
            "cfg_scale": render_request.get("guidance_scale", 0),
            "width": render_request.get("width", 0),
            "height": render_request.get("height", 0),
            "task_id": task_id,
            "created_at": datetime.now().isoformat(),
        }
        sidecar = filepath.with_suffix(".json")
        try:
            sidecar.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        except OSError:
            pass  # Image saved; metadata is best-effort.

        return {"filename": filename, "filepath": str(filepath)}

    async def _check_status(self, task_id: str) -> Dict[str, Any]:
        """Poll a previously-submitted task. Same path as _await_completion."""
        if not task_id:
            return {"success": False, "error": "task_id is required for action=status"}
        return await self._await_completion(task_id, prompt={})

    @staticmethod
    def _safe_json(text: str) -> Any:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None

    # ─── Models ──────────────────────────────────────────────────────

    async def _get_models(self) -> Dict[str, Any]:
        """List available models. EasyDiffusion returns ``{"models": [...]}``."""
        client = self._get_client()
        try:
            response = await client.get("/get/models")
            response.raise_for_status()
            payload = response.json()
            # Server wraps the model list under a `models` key — unwrap so the
            # caller gets a flat list (and stays backwards-compatible).
            models = payload.get("models", payload) if isinstance(payload, dict) else payload
            # Surface a slimmed-down summary so the agent doesn't have to
            # page through tags / paths etc.
            simple: List[Dict[str, Any]] = []
            for m in models if isinstance(models, list) else []:
                if not isinstance(m, dict):
                    continue
                simple.append({
                    "name": m.get("name") or m.get("model", ""),
                    "model": m.get("model", ""),
                    "tags": m.get("tags", []) or [],
                })
            return {"success": True, "models": simple, "count": len(simple)}
        except httpx.HTTPError as e:
            return {"success": False, "error": f"Could not fetch models. Is EasyDiffusion running on {self.base_url}? {e}"}

    # ─── Prompt optimisation (unchanged, retained for callers) ──────

    def build_optimized_prompt(
        self,
        user_request: str,
        style: str = "realistic",
        quality_boosters: bool = True,
    ) -> Dict[str, str]:
        """Map a natural-language request onto an optimised diffusion prompt."""
        base_prompt = user_request.strip()

        style_modifiers = {
            "realistic": "photorealistic, highly detailed, 8k, professional photography",
            "artistic": "digital art, trending on artstation, concept art, masterpiece",
            "anime": "anime style, studio ghibli, makoto shinkai, detailed",
            "cinematic": "cinematic lighting, film grain, anamorphic, dolby vision",
            "sketch": "pencil sketch, detailed linework, concept sketch",
            "painting": "oil painting, textured brush strokes, fine art",
            "3d": "3d render, octane render, unreal engine 5, physically based",
        }

        modifier = style_modifiers.get(style, style_modifiers["realistic"])
        full_prompt = f"{base_prompt}, {modifier}"

        if quality_boosters:
            full_prompt += ", sharp focus, intricate details, beautiful composition"

        negative_prompt = (
            "ugly, deformed, blurry, low resolution, bad anatomy, "
            "watermark, text, signature, extra fingers, mutated hands, "
            "poorly drawn face, distorted, disfigured, bad proportions"
        )

        return {
            "prompt": full_prompt,
            "negative_prompt": negative_prompt,
            "sampler": "dpmpp_2m",
            "steps": 30,
            "cfg_scale": 7.5,
        }

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None