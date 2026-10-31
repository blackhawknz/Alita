"""
Web Browser Tool -- fetch pages, extract readable text, and search the web.

Fetch-type actions are SSRF-guarded: every URL (and every redirect hop) is
resolved and rejected if it points at loopback/private/link-local/metadata
addresses, unless ``ALITA_ALLOW_PRIVATE_URLS=true`` (or the shared
``ALITA_ALLOW_PRIVATE_NETWORK=true``) is set.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

try:
    import httpx
    HAS_HTTPX = True
except ImportError:  # pragma: no cover - httpx is a declared dependency
    httpx = None  # type: ignore[assignment]
    HAS_HTTPX = False

from .http_request import should_block_url

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
}
_SAFE_RESPONSE_HEADERS = {"content-type", "content-length", "last-modified", "content-language"}

MAX_REDIRECTS = 5
MAX_BODY_BYTES = 3_000_000
MAX_HTML_CHARS = 50_000
MAX_TEXT_CHARS = 12_000
REQUEST_TIMEOUT = 30.0


# ─── SSRF guard ────────────────────────────────────────────────────


def allow_private_urls() -> bool:
    """True if the operator opted into fetching private/loopback URLs."""
    for var in ("ALITA_ALLOW_PRIVATE_URLS", "ALITA_ALLOW_PRIVATE_NETWORK"):
        if os.getenv(var, "false").strip().lower() == "true":
            return True
    return False


def check_url_allowed(url: str, allow_private: Optional[bool] = None) -> Optional[str]:
    """Return an error message if ``url`` must not be fetched, else ``None``.

    Blocks non-http(s) schemes and hosts resolving to loopback, private,
    link-local (incl. 169.254.169.254 cloud metadata), multicast, reserved or
    unspecified addresses. Performs DNS resolution (blocking).
    """
    if allow_private is None:
        allow_private = allow_private_urls()
    error = should_block_url(url, allow_private_network=allow_private)
    if error and error.startswith("Refused"):
        error += " ALITA_ALLOW_PRIVATE_URLS=true also permits it for the browser tool."
    return error


# ─── HTML helpers (pure) ───────────────────────────────────────────

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK_STRIP_TAGS = ("script", "style", "noscript", "svg", "template", "iframe", "canvas", "head")
_CHROME_TAGS = ("nav", "footer", "aside", "form")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LI_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_PARA_TAGS_RE = re.compile(
    r"</?(?:p|h[1-6]|blockquote|pre|table|section|article|main|header|footer|nav|aside|"
    r"ul|ol|dl|figure|figcaption|hr|form|fieldset|details|summary)\b[^>]*>",
    re.IGNORECASE,
)
_LINE_TAGS_RE = re.compile(r"</?(?:div|tr|dd|dt|caption|thead|tbody|tfoot|option)\b[^>]*>", re.IGNORECASE)
_PRE_SPLIT_RE = re.compile(r"(<pre\b.*?</pre\s*>)", re.IGNORECASE | re.DOTALL)
_CELL_TAGS_RE = re.compile(r"</?(?:td|th)\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t\f\v\r\u00a0\u200b]+")


def _strip_elements(html: str, tags: Tuple[str, ...]) -> str:
    for tag in tags:
        html = re.sub(
            rf"<{tag}\b[^>]*>.*?</{tag}\s*>", " ", html, flags=re.DOTALL | re.IGNORECASE
        )
        # Drop unterminated / self-closing leftovers of the same tag.
        html = re.sub(rf"<{tag}\b[^>]*/?>", " ", html, flags=re.IGNORECASE)
    return html


def html_to_text(html: str) -> str:
    """Convert HTML to paragraph-preserving plain text.

    Block elements become newlines (paragraph-level ones become blank lines),
    spaces are collapsed within lines, entities are unescaped, and runs of 3+
    newlines are collapsed to a single blank line.
    """
    if not html:
        return ""
    text = _COMMENT_RE.sub(" ", html)
    text = _strip_elements(text, _BLOCK_STRIP_TAGS)
    # Source whitespace (including newlines) is insignificant in HTML except in <pre>.
    parts = _PRE_SPLIT_RE.split(text)
    text = "".join(p if i % 2 else re.sub(r"\s+", " ", p) for i, p in enumerate(parts))
    text = _BR_RE.sub("\n", text)
    text = _LI_RE.sub("\n- ", text)
    text = _PARA_TAGS_RE.sub("\n\n", text)
    text = _LINE_TAGS_RE.sub("\n", text)
    text = _CELL_TAGS_RE.sub(" ", text)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text).replace("\u00b6", "")  # drop headerlink pilcrows
    lines = [_INLINE_WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # A list marker left alone on its line (e.g. "<li><p>x") joins its content.
    text = re.sub(r"^- *\n+(?=\S)", "- ", text, flags=re.MULTILINE)
    return text.strip()


def _largest_block(html: str, tag: str) -> Optional[str]:
    blocks = re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}\s*>", html, flags=re.DOTALL | re.IGNORECASE)
    if not blocks:
        return None
    return max(blocks, key=len)


def extract_readable_html(html: str) -> str:
    """Reduce a page to its main content: drop page chrome and prefer <article>/<main>."""
    if not html:
        return ""
    cleaned = _COMMENT_RE.sub(" ", html)
    cleaned = _strip_elements(cleaned, _BLOCK_STRIP_TAGS)
    for tag in ("article", "main"):
        block = _largest_block(cleaned, tag)
        if block and len(_TAG_RE.sub("", block).strip()) > 200:
            return _strip_elements(block, _CHROME_TAGS)
    body = _largest_block(cleaned, "body") or cleaned
    return _strip_elements(body, _CHROME_TAGS + ("header",))


def extract_readable_text(html: str) -> str:
    """Readable plain text of a page (see :func:`extract_readable_html`)."""
    text = html_to_text(extract_readable_html(html))
    return text or html_to_text(html)


def extract_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title\s*>", html or "", re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return _INLINE_WS_RE.sub(" ", html_lib.unescape(_TAG_RE.sub("", match.group(1)))).replace("\n", " ").strip()


def find_relevant_sections(text: str, query: str, max_sections: int = 10) -> str:
    """Return paragraphs relevant to ``query`` in document order."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return ""
    q = query.lower().strip()
    exact = [i for i, p in enumerate(paragraphs) if q and q in p.lower()]
    if exact:
        chosen = exact[:max_sections]
    else:
        words = [w for w in re.findall(r"\w+", q) if len(w) > 2]
        scored = []
        for i, p in enumerate(paragraphs):
            low = p.lower()
            score = sum(low.count(w) for w in words)
            if score:
                scored.append((score, i))
        if not scored:
            return "\n\n".join(paragraphs[:5])
        chosen = sorted(i for _, i in sorted(scored, reverse=True)[:max_sections])
    return "\n\n".join(paragraphs[i] for i in chosen)


def decode_ddg_url(href: str) -> str:
    """Turn a DuckDuckGo redirect link (``//duckduckgo.com/l/?uddg=...``) into the real URL."""
    href = html_lib.unescape(href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://duckduckgo.com" + href
    parsed = urlparse(href)
    if parsed.netloc.lower().endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target and target[0]:
            return target[0]
    return href


_A_TAG_RE = re.compile(r"<a\b([^>]*)>(.*?)</a\s*>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r"""([a-zA-Z_:-]+)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""")


def _attrs(attr_text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in _ATTR_RE.finditer(attr_text):
        out[m.group(1).lower()] = m.group(3) if m.group(3) is not None else (
            m.group(4) if m.group(4) is not None else m.group(5)
        )
    return out


def _inline_text(fragment: str) -> str:
    return _INLINE_WS_RE.sub(" ", html_lib.unescape(_TAG_RE.sub("", fragment))).replace("\n", " ").strip()


def parse_ddg_results(html: str, max_results: int = 10) -> List[Dict[str, str]]:
    """Parse DuckDuckGo HTML results into [{title, url, snippet}] (ads skipped)."""
    links: List[Tuple[int, int, str, str]] = []
    snippets: List[Tuple[int, str]] = []
    for m in _A_TAG_RE.finditer(html or ""):
        attrs = _attrs(m.group(1))
        classes = attrs.get("class", "").split()
        if "result__a" in classes:
            links.append((m.start(), m.end(), attrs.get("href", ""), m.group(2)))
        elif "result__snippet" in classes:
            snippets.append((m.start(), m.group(2)))
    for m in re.finditer(
        r"<(?:div|td)\b[^>]*class=\"[^\"]*result__snippet[^\"]*\"[^>]*>(.*?)</(?:div|td)\s*>",
        html or "", re.IGNORECASE | re.DOTALL,
    ):
        snippets.append((m.start(), m.group(1)))
    snippets.sort()

    results: List[Dict[str, str]] = []
    seen = set()
    for idx, (start, end, href, title_html) in enumerate(links):
        next_start = links[idx + 1][0] if idx + 1 < len(links) else len(html)
        url = decode_ddg_url(href)
        host = urlparse(url).netloc.lower()
        if not url.startswith(("http://", "https://")) or host.endswith("duckduckgo.com"):
            continue  # ads (duckduckgo.com/y.js) and internal links
        if url in seen:
            continue
        seen.add(url)
        snippet = next((_inline_text(s) for pos, s in snippets if end <= pos < next_start), "")
        results.append({"title": _inline_text(title_html), "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def _charset_from(content_type: str, raw: bytes) -> str:
    m = re.search(r"charset=([\w.-]+)", content_type or "", re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w.-]+)", raw[:4096], re.IGNORECASE)
    if m:
        return m.group(1).decode("ascii", errors="ignore") or "utf-8"
    return "utf-8"


def _decode_body(raw: bytes, content_type: str) -> str:
    charset = _charset_from(content_type, raw)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


# ─── Tool ──────────────────────────────────────────────────────────


class BrowserTool:
    """Fetch web pages, extract readable content, and search via DuckDuckGo."""

    def __init__(self, allow_private: Optional[bool] = None):
        # None -> resolve from the environment on every call.
        self._allow_private = allow_private

    @property
    def definition(self) -> Dict[str, Any]:
        return {
            "name": "browser",
            "description": (
                "Fetch web pages and search the web. Actions: search (DuckDuckGo; "
                "returns title/url/snippet), extract_text (readable main text of a page; "
                "optional `query` keeps only relevant paragraphs), summarize (title + "
                "text preview), fetch (raw HTML, truncated). Private/internal addresses "
                "are blocked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search", "extract_text", "summarize", "fetch"],
                        "description": "The browsing action.",
                    },
                    "url": {
                        "type": "string",
                        "description": "Absolute http(s) URL (fetch / extract_text / summarize).",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query (search) or relevance filter (extract_text).",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "search: number of results (default 10, max 25).",
                        "default": 10,
                    },
                },
                "required": ["action"],
            },
        }

    def _new_client(self) -> "httpx.AsyncClient":
        """A fresh client per call (clients must not be shared across event loops)."""
        return httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT, follow_redirects=False, headers=dict(_DEFAULT_HEADERS)
        )

    async def execute(
        self,
        action: str = "",
        url: str = "",
        query: str = "",
        max_results: int = 10,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute a browsing action; unexpected kwargs are ignored."""
        if not HAS_HTTPX:
            return {"success": False, "error": "httpx not installed. Run: pip install httpx"}
        action = (action or "").strip().lower()
        try:
            if action in ("fetch", "extract_text", "summarize"):
                if not url:
                    return {"success": False, "error": f"url is required for {action}"}
                if action == "fetch":
                    return await self._fetch(url)
                if action == "extract_text":
                    return await self._extract_text(url, query)
                return await self._summarize(url)
            if action == "search":
                if not query:
                    return {"success": False, "error": "query is required for search"}
                try:
                    n = max(1, min(int(max_results or 10), 25))
                except (TypeError, ValueError):
                    n = 10
                return await self._search(query, n)
            return {"success": False, "error": f"Unknown action: {action!r}"}
        except Exception as e:  # noqa: BLE001 - surface to the model
            return {"success": False, "error": f"{type(e).__name__}: {e}"}

    # ─── Fetching ──────────────────────────────────────────────────

    async def _get(self, url: str, *, guard: bool = True) -> Dict[str, Any]:
        """GET ``url`` following redirects manually, SSRF-checking every hop."""
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url if "://" not in url else url
        current = url
        async with self._new_client() as client:
            for _hop in range(MAX_REDIRECTS + 1):
                if guard:
                    block = await asyncio.to_thread(check_url_allowed, current, self._allow_private)
                    if block:
                        return {"success": False, "error": block, "url": current}
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            break
                        current = urljoin(current, location)
                        continue
                    chunks: List[bytes] = []
                    size = 0
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= MAX_BODY_BYTES:
                            truncated = True
                            break
                    raw = b"".join(chunks)[:MAX_BODY_BYTES]
                    content_type = response.headers.get("content-type", "")
                    return {
                        "success": 200 <= response.status_code < 400,
                        "url": str(response.url),
                        "status_code": response.status_code,
                        "content_type": content_type,
                        "headers": {
                            k: v for k, v in response.headers.items()
                            if k.lower() in _SAFE_RESPONSE_HEADERS
                        },
                        "body": _decode_body(raw, content_type),
                        "body_truncated": truncated,
                    }
        return {"success": False, "error": f"Too many redirects (>{MAX_REDIRECTS})", "url": current}

    async def _fetch_page(self, url: str) -> Dict[str, Any]:
        page = await self._get(url)
        if "status_code" in page and not page["success"]:
            page["error"] = f"HTTP {page['status_code']} for {page.get('url', url)}"
        return page

    async def _fetch(self, url: str) -> Dict[str, Any]:
        """Fetch a page and return its (truncated) HTML."""
        page = await self._fetch_page(url)
        if not page.get("success"):
            page.pop("body", None)
            return page
        body = page["body"]
        return {
            "success": True,
            "url": page["url"],
            "status_code": page["status_code"],
            "content_type": page["content_type"],
            "html": body[:MAX_HTML_CHARS],
            "length": len(body),
            "truncated": len(body) > MAX_HTML_CHARS or page.get("body_truncated", False),
            "headers": page["headers"],
        }

    @staticmethod
    def _page_text(page: Dict[str, Any]) -> str:
        ctype = (page.get("content_type") or "").lower()
        body = page.get("body") or ""
        if "html" in ctype or "xml" in ctype or (not ctype and "<html" in body[:2000].lower()):
            return extract_readable_text(body)
        return body

    async def _extract_text(self, url: str, query: str = "") -> Dict[str, Any]:
        """Fetch a page and return its readable text (optionally filtered by query)."""
        page = await self._fetch_page(url)
        if not page.get("success"):
            page.pop("body", None)
            return page
        ctype = (page.get("content_type") or "").lower()
        if ctype and not any(t in ctype for t in ("html", "xml", "text", "json")):
            return {"success": False, "url": page["url"], "error": f"Unsupported content type: {ctype}"}
        text = self._page_text(page)
        relevant = find_relevant_sections(text, query) if query else text
        return {
            "success": True,
            "url": page["url"],
            "title": extract_title(page.get("body") or ""),
            "text": relevant[:MAX_TEXT_CHARS],
            "full_length": len(text),
            "truncated": len(relevant) > MAX_TEXT_CHARS,
        }

    async def _summarize(self, url: str) -> Dict[str, Any]:
        """Fetch a page and return its title and a text preview for summarisation."""
        page = await self._fetch_page(url)
        if not page.get("success"):
            page.pop("body", None)
            return page
        text = self._page_text(page)
        return {
            "success": True,
            "url": page["url"],
            "title": extract_title(page.get("body") or "") or "Unknown",
            "text_length": len(text),
            "preview": text[:3000],
        }

    async def _search(self, query: str, max_results: int = 10) -> Dict[str, Any]:
        """Search via the DuckDuckGo HTML endpoint (no API key)."""
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        try:
            page = await self._get(search_url, guard=False)
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": f"Search request failed: {e}"}
        if not page.get("success"):
            return {
                "success": False,
                "error": page.get("error") or f"Search failed with HTTP {page.get('status_code')}",
            }
        results = parse_ddg_results(page.get("body") or "", max_results)
        out: Dict[str, Any] = {
            "success": True,
            "query": query,
            "results": results,
            "count": len(results),
        }
        if not results:
            out["note"] = "No results parsed (the search engine may have rate-limited the request)."
        return out

    # Backwards-compatible method aliases.
    def _html_to_text(self, html: str) -> str:
        return html_to_text(html)

    def _find_relevant_sections(self, text: str, query: str) -> str:
        return find_relevant_sections(text, query)
