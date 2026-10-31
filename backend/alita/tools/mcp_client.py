"""
MCP Client — minimal Model Context Protocol client.

Speaks JSON-RPC 2.0 over two transports:

* **stdio** — spawns a subprocess and reads/writes newline-delimited JSON-RPC
  messages on its stdin/stdout. Used by virtually all local MCP servers
  (filesystem, git, fetch, sqlite, playwright, etc.).

* **SSE** — connects to a server's SSE stream endpoint, POSTs requests to
  the message URL advertised by the stream, and reads responses from the
  stream. Used by remote/hosted MCP servers (and the older SSE-based
  reference servers).

No dependency on the upstream ``mcp`` PyPI package — the wire protocol is
small and stable enough to implement directly. Keeping it in-tree avoids
pulling a heavy dependency chain and keeps the agent fully self-contained.

Lifecycle:
    client = StdioMCPClient("filesystem", ["npx","-y", "@modelcontextprotocol/server-filesystem", "/tmp"])
    await client.connect()
    tools = await client.list_tools()              # list of MCPTool
    result = await client.call_tool("read_file", {"path": "/tmp/x"})
    await client.close()                           # terminate subprocess

Protocol reference: https://modelcontextprotocol.io/specification
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import urljoin

try:
    import httpx
    HAS_HTTPX = True
except ImportError:  # pragma: no cover - httpx is a declared dependency
    HAS_HTTPX = False

logger = logging.getLogger(__name__)

#: StreamReader buffer limit for stdio servers (large tool results arrive as one line).
STDIO_READ_LIMIT = 16 * 1024 * 1024

#: Number of stderr lines retained from stdio servers for error messages.
STDERR_TAIL_LINES = 50


# ─── Data shapes ───────────────────────────────────────────────────


@dataclass
class MCPTool:
    """A single tool exposed by an MCP server."""

    name: str
    description: str
    input_schema: Dict[str, Any]

    def to_openai_function(self) -> Dict[str, Any]:
        """Convert to OpenAI-compatible function tool definition."""
        return {
            "name": self.name,
            "description": self.description or f"MCP tool {self.name}",
            "parameters": self.input_schema or {"type": "object", "properties": {}},
        }


@dataclass
class MCPToolResult:
    """Normalised result of an MCP tool call."""

    success: bool
    text: str = ""
    error: str = ""
    raw: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"success": self.success}
        if self.text:
            out["text"] = self.text
        if self.error:
            out["error"] = self.error
        if self.raw:
            out["content"] = self.raw
        return out


# ─── JSON-RPC errors ───────────────────────────────────────────────


class RPCError(Exception):
    """Raised when an MCP server returns a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class MCPConnectionError(Exception):
    """Raised when a client cannot establish or maintain an MCP connection."""


# ─── Base client ───────────────────────────────────────────────────


class _BaseMCPClient:
    """Common JSON-RPC plumbing for all transports."""

    PROTOCOL_VERSION = "2024-11-05"
    CLIENT_NAME = "alita"
    CLIENT_VERSION = "0.1.0"

    def __init__(self, name: str):
        self.name = name
        self._next_id = 1
        self._pending: Dict[Any, "asyncio.Future[Any]"] = {}
        self._initialized = False
        self._server_capabilities: Dict[str, Any] = {}
        self._server_info: Dict[str, Any] = {}
        self._background: "set[asyncio.Task]" = set()

    # – Transport hooks (overridden by subclasses) ──────────────────

    async def _send(self, payload: bytes) -> None:
        raise NotImplementedError

    async def _recv(self) -> Optional[bytes]:
        raise NotImplementedError

    async def _start(self) -> None:
        raise NotImplementedError

    async def _stop(self) -> None:
        raise NotImplementedError

    def _error_context(self) -> str:
        """Extra diagnostic text appended to connection errors (e.g. stderr tail)."""
        return ""

    def _fail_pending(self, message: str) -> None:
        exc = MCPConnectionError(message + self._error_context())
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # – Message routing ─────────────────────────────────────────────

    async def _consumer(self) -> None:
        """Background task: read incoming messages and resolve futures."""
        while True:
            try:
                raw = await self._recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - broken pipe / EOF
                self._fail_pending(f"transport closed: {exc}")
                return
            if raw is None:
                continue
            try:
                msg = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(msg, list):  # JSON-RPC batch
                for item in msg:
                    if isinstance(item, dict):
                        self._dispatch(item)
            elif isinstance(msg, dict):
                self._dispatch(msg)

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        """Route a decoded JSON-RPC message to its waiting future (if any)."""
        msg_id = msg.get("id")
        if msg_id is None:
            return  # notification
        if "method" in msg:
            # Server-initiated request: answer ping, reject everything else so
            # the server never waits on us.
            if msg.get("method") == "ping":
                reply: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
            else:
                reply = {
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not supported: {msg.get('method')}"},
                }
            task = asyncio.get_running_loop().create_task(self._safe_send(reply))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            return
        fut = self._pending.pop(msg_id, None)
        if fut is None or fut.done():
            return
        if "error" in msg:
            err = msg["error"] or {}
            fut.set_exception(RPCError(err.get("code", -1), err.get("message", "unknown"), err.get("data")))
        else:
            fut.set_result(msg.get("result"))

    async def _safe_send(self, payload: Dict[str, Any]) -> None:
        try:
            await self._send((json.dumps(payload) + "\n").encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("MCP %s: failed to reply to server request: %s", self.name, exc)

    async def _request(self, method: str, params: Optional[Dict[str, Any]] = None,
                       timeout: float = 30.0) -> Any:
        """Send a JSON-RPC request and await its response."""
        if not self._initialized and method != "initialize":
            raise MCPConnectionError(f"Cannot send {method}: client not initialized")
        req_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        fut: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._send((json.dumps(payload) + "\n").encode("utf-8"))
        except BaseException:
            self._pending.pop(req_id, None)
            raise
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise MCPConnectionError(
                f"Timed out waiting for {method} response{self._error_context()}"
            ) from exc

    async def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send((json.dumps(payload) + "\n").encode("utf-8"))

    # – Public MCP API ──────────────────────────────────────────────

    async def connect(self, timeout: float = 30.0) -> None:
        """Perform the MCP handshake. Tears the transport down again on failure."""
        try:
            await self._start()
            result = await self._request("initialize", {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self.CLIENT_NAME, "version": self.CLIENT_VERSION},
            }, timeout=timeout)
            if isinstance(result, dict):
                self._server_capabilities = result.get("capabilities", {}) or {}
                self._server_info = result.get("serverInfo", {}) or {}
            # Per spec, the client must send the `initialized` notification next.
            await self._notify("notifications/initialized")
        except BaseException:
            await self.close()
            raise
        self._initialized = True

    async def list_tools(self, timeout: float = 30.0) -> List[MCPTool]:
        """List the tools exposed by the server."""
        result = await self._request("tools/list", {}, timeout=timeout)
        tools_raw = (result or {}).get("tools", []) if isinstance(result, dict) else []
        out: List[MCPTool] = []
        for t in tools_raw:
            if not isinstance(t, dict):
                continue
            out.append(MCPTool(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema") or {"type": "object", "properties": {}},
            ))
        return out

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any],
                        timeout: float = 120.0) -> MCPToolResult:
        """Invoke a tool and return a normalised result."""
        try:
            result = await self._request("tools/call", {
                "name": tool_name,
                "arguments": arguments or {},
            }, timeout=timeout)
        except RPCError as exc:
            return MCPToolResult(success=False, error=str(exc))
        if not isinstance(result, dict):
            return MCPToolResult(success=False, error=f"unexpected result: {result!r}")
        is_error = bool(result.get("isError"))
        content = result.get("content") or []
        text_parts: List[str] = []
        for entry in content:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "text":
                text_parts.append(entry.get("text", ""))
            elif entry.get("type") == "image":
                # Surface a placeholder so the model knows an image was returned
                # without dumping base64 into context.
                text_parts.append(f"[image: {entry.get('mimeType','image')}, {len(entry.get('data','') or '')} bytes]")
        return MCPToolResult(
            success=not is_error,
            text="\n".join(text_parts),
            error="" if not is_error else "\n".join(text_parts) or "tool reported an error",
            raw=content if isinstance(content, list) else [],
        )

    async def ping(self, timeout: float = 10.0) -> bool:
        try:
            await self._request("ping", {}, timeout=timeout)
            return True
        except (RPCError, MCPConnectionError, asyncio.TimeoutError):
            return False

    @property
    def server_info(self) -> Dict[str, Any]:
        return dict(self._server_info)

    async def close(self) -> None:
        """Tear down the connection. Safe to call multiple times."""
        self._initialized = False
        try:
            await self._stop()
        except Exception:
            pass
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        for task in list(self._background):
            task.cancel()
        self._background.clear()


# ─── stdio transport ───────────────────────────────────────────────


class StdioMCPClient(_BaseMCPClient):
    """
    MCP client over a subprocess's stdin/stdout (newline-delimited JSON-RPC).

    Mirrors the reference ``@modelcontextprotocol/server-*`` servers which
    expect a Node.js subprocess spoken to over stdio.
    """

    def __init__(self, name: str, command: str, args: List[str],
                 env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None):
        super().__init__(name)
        self.command = command
        self.args = list(args)
        self.env = env
        self.cwd = cwd
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stderr_tail: Deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)

    def _resolve_command(self) -> str:
        """Resolve the executable via PATH (on Windows ``npx`` -> ``npx.cmd``)."""
        return shutil.which(self.command) or self.command

    @property
    def stderr_tail(self) -> List[str]:
        """The most recent stderr lines emitted by the server."""
        return list(self._stderr_tail)

    def _error_context(self) -> str:
        if not self._stderr_tail:
            return ""
        tail = "\n".join(list(self._stderr_tail)[-10:])
        return f"\nServer stderr (last lines):\n{tail}"

    async def _start(self) -> None:
        env = os.environ.copy()
        if self.env:
            env.update({str(k): str(v) for k, v in self.env.items()})
        extra: Dict[str, Any] = {}
        if os.name == "nt":
            extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        command = self._resolve_command()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.cwd,
                limit=STDIO_READ_LIMIT,
                **extra,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise MCPConnectionError(
                f"Could not start MCP command {self.command!r}: {exc}. "
                f"Ensure the binary is installed and on PATH."
            ) from exc
        loop = asyncio.get_running_loop()
        self._stderr_task = loop.create_task(self._drain_stderr())
        self._consumer_task = loop.create_task(self._consumer())

    async def _drain_stderr(self) -> None:
        """Continuously read stderr so the server never blocks on a full pipe."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text[:2000])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - e.g. over-long line; keep draining raw
            try:
                while await proc.stderr.read(65536):
                    pass
            except Exception:  # noqa: BLE001
                return

    async def _send(self, payload: bytes) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPConnectionError("subprocess not started")
        try:
            self._proc.stdin.write(payload)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise MCPConnectionError("subprocess stdin closed") from exc

    async def _recv(self) -> Optional[bytes]:
        if self._proc is None or self._proc.stdout is None:
            raise MCPConnectionError("subprocess not started")
        line = await self._proc.stdout.readline()
        if not line:
            # EOF
            raise MCPConnectionError("subprocess stdout EOF")
        return line

    async def _stop(self) -> None:
        for attr in ("_consumer_task", "_stderr_task"):
            task: Optional[asyncio.Task] = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                setattr(self, attr, None)
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        # Well-behaved servers exit on stdin EOF; give them a moment first.
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
            return
        except asyncio.TimeoutError:
            pass
        await self._kill_tree(proc)

    @staticmethod
    async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        """Terminate the server and its children (``npx`` spawns node under cmd.exe)."""
        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                await asyncio.wait_for(killer.wait(), timeout=10.0)
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
                return
            except (ProcessLookupError, asyncio.TimeoutError):
                pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass


# ─── SSE transport ─────────────────────────────────────────────────


#: Sentinel queued when the SSE stream ends so the consumer stops.
_STREAM_CLOSED = b""


class SSEMCPClient(_BaseMCPClient):
    """
    MCP client over the SSE+POST transport.

    Connects to the server's SSE endpoint, learns the message-POST URL from
    the server's first ``endpoint`` event, then sends JSON-RPC requests as
    POSTs to that URL and reads responses from the stream (or, for servers
    that answer inline, from the POST response body).
    """

    def __init__(self, name: str, url: str, headers: Optional[Dict[str, str]] = None,
                 timeout: float = 30.0):
        super().__init__(name)
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout
        self._post_url: Optional[str] = None
        self._client: Optional["httpx.AsyncClient"] = None
        self._stream_task: Optional[asyncio.Task] = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._stream_response: Optional["httpx.Response"] = None
        self._queue: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._ready = asyncio.Event()
        self._stream_error = ""

    async def _start(self) -> None:
        if not HAS_HTTPX:
            raise MCPConnectionError("httpx not installed")
        # No read timeout: the SSE stream may legitimately stay idle for long periods.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, read=None), headers=self.headers
        )
        try:
            self._stream_response = await self._client.send(
                self._client.build_request("GET", self.url, headers={"Accept": "text/event-stream"}),
                stream=True,
            )
        except httpx.RequestError as exc:
            raise MCPConnectionError(f"Could not open SSE stream to {self.url}: {exc}") from exc
        if self._stream_response.status_code != 200:
            body = (await self._stream_response.aread()).decode("utf-8", errors="replace")[:200]
            status = self._stream_response.status_code
            await self._stream_response.aclose()
            raise MCPConnectionError(f"SSE endpoint {self.url} returned {status}: {body}")

        loop = asyncio.get_running_loop()
        self._stream_task = loop.create_task(self._stream_loop())
        self._consumer_task = loop.create_task(self._consumer())

        # Wait for the ``endpoint`` event before allowing the handshake to proceed.
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise MCPConnectionError(
                f"SSE server {self.url} did not send an `endpoint` event within {self.timeout}s"
            ) from exc
        if self._post_url is None:
            raise MCPConnectionError(f"SSE stream {self.url} closed before an endpoint was received")

    def _handle_event(self, event_type: str, data_lines: List[str]) -> Optional[bytes]:
        """Process one complete SSE event; returns a payload to enqueue, if any."""
        if not data_lines:
            return None
        if event_type == "endpoint":
            base = str(self._stream_response.url) if self._stream_response is not None else self.url
            self._post_url = urljoin(base, "".join(data_lines).strip())
            self._ready.set()
            return None
        if event_type in ("", "message"):
            return "\n".join(data_lines).encode("utf-8")
        return None

    async def _stream_loop(self) -> None:
        """Parse SSE bytes into events and enqueue JSON-RPC payloads."""
        assert self._stream_response is not None
        buffer = b""
        event_type = ""
        data_lines: List[str] = []
        try:
            async for chunk in self._stream_response.aiter_bytes():
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.rstrip(b"\r")
                    if not line:
                        payload = self._handle_event(event_type, data_lines)
                        if payload:
                            await self._queue.put(payload)
                        event_type = ""
                        data_lines = []
                        continue
                    if line.startswith(b"event:"):
                        event_type = line[6:].strip().decode("utf-8", errors="replace")
                    elif line.startswith(b"data:"):
                        value = line[5:]
                        if value.startswith(b" "):
                            value = value[1:]
                        data_lines.append(value.decode("utf-8", errors="replace"))
                    elif line.startswith(b":"):
                        pass  # SSE comment / heartbeat
                    else:
                        # Some servers send raw JSON lines without a `data:` prefix.
                        text = line.decode("utf-8", errors="replace")
                        try:
                            json.loads(text)
                            data_lines.append(text)
                        except json.JSONDecodeError:
                            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - network errors end the stream
            self._stream_error = str(exc)
        finally:
            self._ready.set()  # unblock _start if the stream died early
            self._queue.put_nowait(_STREAM_CLOSED)

    def _error_context(self) -> str:
        return f" ({self._stream_error})" if self._stream_error else ""

    async def _send(self, payload: bytes) -> None:
        if self._client is None or self._post_url is None:
            raise MCPConnectionError("SSE client not initialised (no endpoint URL)")
        try:
            # SSE POST bodies are bare JSON (no trailing newline).
            resp = await self._client.post(
                self._post_url,
                content=payload.rstrip(b"\n"),
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                timeout=self.timeout,
            )
        except httpx.RequestError as exc:
            raise MCPConnectionError(f"POST to {self._post_url} failed: {exc}") from exc
        # 202 Accepted: the response arrives via the stream.
        if resp.status_code not in (200, 202, 204):
            raise MCPConnectionError(f"POST to {self._post_url} failed: {resp.status_code}")
        if resp.status_code == 200 and "json" in resp.headers.get("content-type", "").lower():
            body = resp.content.strip()
            if body:
                await self._queue.put(body)

    async def _recv(self) -> Optional[bytes]:
        item = await self._queue.get()
        if item == _STREAM_CLOSED:
            raise MCPConnectionError("SSE stream closed")
        return item

    async def _stop(self) -> None:
        for attr in ("_stream_task", "_consumer_task"):
            task: Optional[asyncio.Task] = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                setattr(self, attr, None)
        if self._stream_response is not None:
            try:
                await self._stream_response.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._stream_response = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

