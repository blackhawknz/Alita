"""Network-free tests for browser helpers: text extraction, DDG parsing, SSRF guard."""

import pytest

from alita.tools.browser import (
    BrowserTool,
    check_url_allowed,
    decode_ddg_url,
    extract_readable_text,
    find_relevant_sections,
    html_to_text,
    parse_ddg_results,
)


def test_html_to_text_preserves_paragraphs():
    html = """
    <html><head><title>T</title><style>.x{}</style></head><body>
      <h1>Heading</h1>
      <p>First   paragraph
         spans lines &amp; has entities&nbsp;here.</p>
      <p>Second paragraph.</p>
      <ul><li>one</li><li>two</li></ul>
      <div>Line A<br>Line B</div>
      <script>var x = 1;</script>
    </body></html>
    """
    text = html_to_text(html)
    paragraphs = text.split("\n\n")
    assert "Heading" in paragraphs[0]
    assert "First paragraph spans lines & has entities here." in text
    assert "Second paragraph." in paragraphs
    assert "- one\n- two" in text
    assert "Line A\nLine B" in text
    assert "var x" not in text
    assert "\n\n\n" not in text


def test_find_relevant_sections_works_on_extracted_text():
    html = "<p>Intro about cats.</p><p>Python is a language.</p><p>Dogs bark.</p>"
    text = html_to_text(html)
    assert find_relevant_sections(text, "python") == "Python is a language."


def test_readable_extraction_prefers_article_and_drops_chrome():
    body = "Main article text. " * 20
    html = f"""
    <body><nav>Home | About</nav><header>Site header</header>
    <article><h1>Title</h1><p>{body}</p></article>
    <aside>Related links</aside><footer>Copyright</footer></body>
    """
    text = extract_readable_text(html)
    assert "Main article text." in text
    for junk in ("Home | About", "Related links", "Copyright", "Site header"):
        assert junk not in text


def test_decode_ddg_url():
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpath%3Fa%3D1%26b%3D2&amp;rut=abc"
    assert decode_ddg_url(href) == "https://example.com/path?a=1&b=2"
    assert decode_ddg_url("https://example.org/x") == "https://example.org/x"


def test_parse_ddg_results():
    html = """
    <div class="result results_links result--ad"><a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=x">Ad</a></div>
    <div class="result"><h2><a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&amp;rut=1">Python <b>Docs</b></a></h2>
       <a class="result__snippet" href="x">The official &quot;Python&quot; docs.</a></div>
    <div class="result"><a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpypi.org%2F" class="result__a">PyPI</a>
       <div class="result__snippet">Package index</div></div>
    """
    results = parse_ddg_results(html)
    assert results == [
        {"title": "Python Docs", "url": "https://docs.python.org/3/", "snippet": 'The official "Python" docs.'},
        {"title": "PyPI", "url": "https://pypi.org/", "snippet": "Package index"},
    ]


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://10.0.0.5/",
    "http://0.0.0.0/",
    "file:///etc/passwd",
])
def test_ssrf_guard_blocks_internal_targets(url):
    assert check_url_allowed(url, allow_private=False) is not None


def test_ssrf_guard_opt_in(monkeypatch):
    assert check_url_allowed("http://127.0.0.1/", allow_private=True) is None
    monkeypatch.setenv("ALITA_ALLOW_PRIVATE_URLS", "true")
    assert check_url_allowed("http://127.0.0.1/") is None


async def test_browser_fetch_blocks_metadata_without_network(monkeypatch):
    monkeypatch.delenv("ALITA_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.delenv("ALITA_ALLOW_PRIVATE_NETWORK", raising=False)
    tool = BrowserTool()
    res = await tool.execute(action="fetch", url="http://169.254.169.254/latest/", junk=1)
    assert res["success"] is False
    assert "Refused" in res["error"]
