"""Web access for Aasaan: reading URLs the student gives, and finding verified resources for chapters.

Reading a URL (`fetch_page`), cheapest tier first, behind ONE function so a headless option can be added later:
  Tier 0  httpx + trafilatura                       (no external service)
  Tier 1  Jina Reader  GET https://r.jina.ai/<url>  (works without a key, rate limited; JINA_API_KEY raises the quota)
  Tier 2  Firecrawl    POST https://api.firecrawl.dev/v2/scrape   (only if FIRECRAWL_API_KEY is set)
If every tier yields under ~300 characters we say so and log it. Pages are cached by URL hash.

Rules: http/https only, public hosts only (SSRF guard), robots.txt respected, a clear User-Agent, 8-10 s timeouts,
a small per-domain concurrency limit. Third-party content is untrusted data: we store titles, short snippets and
links, never whole pages in the student's notes.
"""
from __future__ import annotations

import hashlib
import html as htmllib
import ipaddress
import logging
import os
import re
import socket
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote, urlparse

import httpx

from .tools import ToolFailure, ToolResult, ToolRunner, check_response

log = logging.getLogger("aasaan.web")

MIN_TEXT_CHARS = 300
TIMEOUT_S = 9.0
CONTACT = os.environ.get("AASAAN_CONTACT", "https://aasaan.local")
USER_AGENT = f"AasaanBot/1.0 (+{CONTACT}; educational tutor; respects robots.txt)"
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


# ------------------------------------------------------------------ http (patchable in tests)
def http_get(url: str, *, headers: Optional[dict] = None, params: Optional[dict] = None, timeout: float = TIMEOUT_S):
    h = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5"}
    h.update(headers or {})
    return httpx.get(url, headers=h, params=params, timeout=timeout, follow_redirects=True)


def http_post(url: str, *, headers: Optional[dict] = None, json: Any = None, timeout: float = 30.0):
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    return httpx.post(url, headers=h, json=json, timeout=timeout)


_resolve = socket.getaddrinfo  # patchable in tests

# ---- ToolRunner: Wikipedia, Commons, Jina, Firecrawl, YouTube and oEmbed calls go through it (events, retries, breakers, budgets).
# Plain page fetches and link verification talk to arbitrary hosts, where a dead page is normal, so they stay direct.
RUNNER: Optional[ToolRunner] = None


def set_runner(runner: Optional[ToolRunner]) -> None:
    global RUNNER
    RUNNER = runner


# page_health gate: (url, title, text) -> (healthy, reason). Set by the engine; None = no gate (plain length check only).
PAGE_GATE: Optional[Callable[[str, str, str], tuple]] = None


def set_page_gate(fn: Optional[Callable[[str, str, str], tuple]]) -> None:
    global PAGE_GATE
    PAGE_GATE = fn


def _page_ok(url: str, title: str, text: str, tier: int, errors: list) -> bool:
    """Is what this tier returned the real page? A wall/login/CAPTCHA/error page makes the fetcher try the next tier."""
    if PAGE_GATE is None:
        return True
    try:
        healthy, reason = PAGE_GATE(url, title, text)
    except Exception as e:  # a broken gate must never stop reading pages
        log.warning("[aasaan] page_health gate failed: %s", e)
        return True
    if not healthy:
        errors.append(f"tier{tier}: page_health: {reason}")
        log.info("[aasaan] page_health rejected tier %d for %s: %s", tier, url, reason)
    return healthy


def tool(name: str, fn, **kw) -> ToolResult:
    global RUNNER
    if RUNNER is None:
        RUNNER = ToolRunner(None)  # standalone use (no store): events are dropped, breakers and budgets still apply
    return RUNNER.run(name, fn, **kw)


def _json(resp, what: str):
    check_response(resp, what)
    try:
        return resp.json()
    except ValueError:
        raise ToolFailure("invalid", f"{what}: response was not JSON", False)


def _thin(min_chars: int):
    def check(d: dict) -> None:
        n = len((d or {}).get("text", ""))
        if n < min_chars:
            raise ToolFailure("thin", f"only {n} characters of content (need {min_chars})", False)
    return check


def extract_urls(text: str) -> list[str]:
    seen, out = set(), []
    for m in URL_RE.findall(text or ""):
        u = m.rstrip(".,;:!?")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def is_safe_url(url: str) -> tuple[bool, str]:
    """SSRF guard: only http(s) to public addresses."""
    try:
        p = urlparse(url)
    except ValueError:
        return False, "malformed URL"
    if p.scheme not in ("http", "https") or not p.hostname:
        return False, "only http(s) URLs are allowed"
    host = p.hostname.lower()
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".internal") or host.endswith(".local"):
        return False, "local addresses are not allowed"
    try:
        infos = _resolve(host, p.port or (443 if p.scheme == "https" else 80))
    except OSError as e:
        return False, f"cannot resolve host: {e}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False, "address is not public"
    return True, ""


# ------------------------------------------------------------------ politeness
_domain_sems: dict[str, threading.Semaphore] = {}
_domain_lock = threading.Lock()
_robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser]] = {}


def _domain_sem(host: str) -> threading.Semaphore:
    with _domain_lock:
        return _domain_sems.setdefault(host, threading.Semaphore(2))


def allowed_by_robots(url: str) -> bool:
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    now = time.time()
    cached = _robots.get(origin)
    if not cached or now - cached[0] > 3600:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = http_get(origin + "/robots.txt", timeout=5)
            if r.status_code == 200 and "text" in r.headers.get("content-type", "text/plain"):
                rp.parse(r.text.splitlines())
            else:
                rp.parse([])  # no usable robots.txt: allowed
        except Exception:
            rp.parse([])
        _robots[origin] = (now, rp)
        cached = _robots[origin]
    return cached[1].can_fetch("AasaanBot", url)


# --------------------------------------------------------------------- reading
@dataclass
class PageResult:
    ok: bool
    url: str
    title: str = ""
    text: str = ""
    description: str = ""
    tier: int = -1
    error: str = ""


def clean_text(s: str) -> str:
    """Titles/descriptions shown to students: decode HTML entities, replace broken-encoding characters, squash whitespace."""
    return re.sub(r"\s+", " ", htmllib.unescape(s or "").replace("\ufffd", "-")).strip()


def _html_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return clean_text(m.group(1)) if m else ""


def _meta_description(html: str) -> str:
    for pat in (r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']'):
        m = re.search(pat, html, re.I | re.S)
        if m:
            return clean_text(m.group(1))
    return ""


def extract_text(html: str) -> str:
    try:
        import trafilatura
        return (trafilatura.extract(html, include_comments=False, include_tables=False, favor_recall=True) or "").replace("\ufffd", "").strip()
    except Exception as e:  # extraction must never break a build
        log.warning("[aasaan] trafilatura failed: %s", e)
        return ""


def _cache_key(url: str) -> str:
    return "page:" + hashlib.sha1(url.encode()).hexdigest()


def fetch_page(url: str, store=None) -> PageResult:
    """Read one URL into text. See module docstring for tiers. Never raises."""
    if store:
        hit = store.cache_get(_cache_key(url))
        if hit:
            return PageResult(**hit)
    ok, why = is_safe_url(url)
    if not ok:
        log.warning("[aasaan] fetch_page refused %s: %s", url, why)
        return PageResult(False, url, error=why)
    if not allowed_by_robots(url):
        log.warning("[aasaan] fetch_page %s disallowed by robots.txt", url)
        return PageResult(False, url, error="robots.txt does not allow reading this page")

    host = urlparse(url).hostname or ""
    errors: list[str] = []
    with _domain_sem(host):
        # Tier 0
        try:
            r = http_get(url)
            ctype = r.headers.get("content-type", "")
            if r.status_code < 400 and ("html" in ctype or "text" in ctype):
                text = extract_text(r.text)
                if len(text) >= MIN_TEXT_CHARS:
                    if _page_ok(url, _html_title(r.text), text, 0, errors):
                        return _cache(store, PageResult(True, str(getattr(r, "url", url)), _html_title(r.text), text[:20000],
                                                        _meta_description(r.text), 0))
                else:
                    errors.append(f"tier0: only {len(text)} chars")
            else:
                errors.append(f"tier0: HTTP {r.status_code} {ctype}")
        except Exception as e:
            errors.append(f"tier0: {type(e).__name__}: {e}")
        # Tier 1: Jina Reader (works without a key; JINA_API_KEY raises the quota)
        def jina():
            headers = {"Accept": "text/plain"}
            if os.environ.get("JINA_API_KEY"):
                headers["Authorization"] = f"Bearer {os.environ['JINA_API_KEY']}"
            r = http_get("https://r.jina.ai/" + url, headers=headers, timeout=15)
            check_response(r, "jina")
            title, body = _parse_jina(r.text)
            return {"title": title, "text": body}

        res = tool("jina", jina, validate=_thin(MIN_TEXT_CHARS), summary=f"jina read {host}")
        if res.ok and _page_ok(url, res.data["title"], res.data["text"], 1, errors):
            return _cache(store, PageResult(True, url, res.data["title"], res.data["text"][:20000], "", 1))
        if not res.ok:
            errors.append(f"tier1 jina: [{res.error.kind}] {res.error.message}")
        # Tier 2: Firecrawl (last tier; needs FIRECRAWL_API_KEY; capped per notebook; off for the session after a 401/402)
        def firecrawl():
            r = http_post("https://api.firecrawl.dev/v2/scrape", headers={"Authorization": f"Bearer {os.environ.get('FIRECRAWL_API_KEY', '')}"},
                          json={"url": url, "formats": ["markdown"], "onlyMainContent": True}, timeout=40)
            check_response(r, "firecrawl scrape")
            data = (r.json() or {}).get("data", {})
            return {"title": (data.get("metadata") or {}).get("title", ""), "text": (data.get("markdown") or "").strip()}

        res = tool("firecrawl", firecrawl, validate=_thin(MIN_TEXT_CHARS), summary=f"firecrawl scrape {host}")
        if res.ok and _page_ok(url, res.data["title"], res.data["text"], 2, errors):
            return _cache(store, PageResult(True, url, res.data["title"], res.data["text"][:20000], "", 2))
        if not res.ok and res.error.kind != "unconfigured":
            errors.append(f"tier2 firecrawl: [{res.error.kind}] {res.error.message}")
    msg = "I couldn't read that page, please paste the text"
    log.warning("[aasaan] fetch_page failed for %s: %s", url, "; ".join(errors))
    return PageResult(False, url, error=f"{msg} ({'; '.join(errors)[:300]})")


def _parse_jina(text: str) -> tuple[str, str]:
    title = ""
    m = re.match(r"\s*Title:\s*(.*?)\n", text)
    if m:
        title = m.group(1).strip()
    if "Markdown Content:" in text:
        text = text.split("Markdown Content:", 1)[1]
    return title, text.strip()


def _cache(store, res: PageResult) -> PageResult:
    if store:
        store.cache_set(_cache_key(res.url), res.__dict__)
    return res


# ================================================================== resources
@dataclass
class Candidate:
    id: str
    kind: str                       # link | video | image
    url: str
    title: str = ""
    source: str = ""                # domain or provider
    reason: str = ""                # one short line (page description), never a copy of the page
    extra: dict = field(default_factory=dict)
    verified: bool = False


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def snippet(text: str, n: int = 160) -> str:
    """A short one-line reason: cut at a word boundary, add an ellipsis. Never a copy of the page."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


# -------------------------------------------------------------------- discovery
def discover_wikipedia(query: str, n: int = 3) -> list[Candidate]:
    def search():
        data = _json(http_get("https://en.wikipedia.org/w/api.php",
                              params={"action": "opensearch", "search": query, "limit": n, "namespace": 0, "format": "json"}), "wikipedia search")
        return {"hits": [{"title": t, "url": u} for t, u in zip(data[1], data[3])]}

    res = tool("wikipedia", search, cache_key=f"search:{n}:{query.lower()}", summary=f"wikipedia search '{query[:40]}'")
    return [Candidate(f"w{i}", "link", h["url"], h["title"], "wikipedia.org") for i, h in enumerate(res.value_or({}).get("hits", []))]


def discover_books(query: str, n: int = 5) -> list[Candidate]:
    """Real books on a topic from Open Library (free, no key). Each Candidate is a book page you can open, with its facts in `extra`
    (authors, year, subjects, and an Internet Archive id when a copy can be read online). Nothing here is copied from the books."""
    def search():
        data = _json(http_get("https://openlibrary.org/search.json", params={
            "q": query, "limit": max(n * 3, 10), "fields": "key,title,author_name,first_publish_year,subject,first_sentence,ia,edition_count,language"}), "openlibrary search")
        return {"docs": data.get("docs", [])}

    res = tool("openlibrary", search, cache_key=f"books:{n}:{query.lower()}", summary=f"open library books '{query[:40]}'")
    docs = [d for d in res.value_or({}).get("docs", []) if d.get("key") and d.get("title")]
    docs.sort(key=lambda d: -(d.get("edition_count") or 0))             # widely published books first
    out, seen = [], set()
    for d in docs:
        t = d["title"].strip().lower()
        if t in seen:
            continue
        seen.add(t)
        sentence = d.get("first_sentence")
        sentence = sentence[0] if isinstance(sentence, list) and sentence else (sentence if isinstance(sentence, str) else "")
        out.append(Candidate(f"b{len(out)}", "book", "https://openlibrary.org" + d["key"], d["title"].strip(), "openlibrary.org",
                             extra={"authors": (d.get("author_name") or [])[:3], "year": d.get("first_publish_year"), "subjects": [s for s in (d.get("subject") or []) if len(s) < 40][:6],
                                    "first_sentence": sentence[:240], "ia": (d.get("ia") or [""])[0]}))
        if len(out) >= n:
            break
    return out


def discover_youtube(query: str, n: int = 4) -> list[Candidate]:
    def search():
        data = _json(http_get("https://www.googleapis.com/youtube/v3/search", params={
            "part": "snippet", "q": query, "type": "video", "maxResults": n, "videoEmbeddable": "true", "safeSearch": "strict",
            "key": os.environ.get("YOUTUBE_API_KEY", "")}), "youtube search")
        return {"videos": [{"id": it["id"]["videoId"], "title": it["snippet"]["title"], "channel": it["snippet"].get("channelTitle", "YouTube")}
                           for it in data.get("items", [])]}

    res = tool("youtube", search, cache_key=f"search:{n}:{query.lower()}", summary=f"youtube search '{query[:40]}'")
    return [Candidate(f"y{i}", "video", f"https://www.youtube.com/watch?v={v['id']}", v["title"], v["channel"], extra={"video_id": v["id"]})
            for i, v in enumerate(res.value_or({}).get("videos", []))]


def video_markers(video_id: str) -> list[dict]:
    """Chapter markers from the video description (YouTube Data API, same key and budget as search). [] when there are none."""
    from .agents.gates import parse_markers

    def fetch():
        r = http_get("https://www.googleapis.com/youtube/v3/videos", params={"part": "snippet", "id": video_id, "key": os.environ.get("YOUTUBE_API_KEY", "")})
        items = _json(r, "youtube video").get("items") or []
        return {"description": ((items[0].get("snippet") or {}).get("description", "") if items else "")}

    res = tool("youtube", fetch, cache_key=f"markers:{video_id}", summary=f"youtube description {video_id}")
    return parse_markers(res.value_or({}).get("description", ""))


def youtube_id(url: str) -> Optional[str]:
    p = urlparse(url)
    host = (p.hostname or "").removeprefix("www.")
    if host == "youtu.be":
        return p.path.strip("/").split("/")[0] or None
    if host in ("youtube.com", "m.youtube.com", "music.youtube.com") and p.path == "/watch":
        m = re.search(r"[?&]v=([\w-]{11})", "?" + p.query)
        return m.group(1) if m else None
    return None


def discover_images(query: str, n: int = 4) -> list[Candidate]:
    """Wikimedia Commons (no key). Hotlinked thumbnails + attribution only; nothing is copied to our server."""
    def search():
        r = http_get("https://commons.wikimedia.org/w/api.php", params={
            "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": 6, "gsrlimit": n * 2, "prop": "imageinfo", "iiprop": "url|extmetadata|mime|size", "iiurlwidth": 800})
        pages = (_json(r, "commons search").get("query") or {}).get("pages") or {}
        found = []
        for pg in sorted(pages.values(), key=lambda p: p.get("index", 0)):
            ii = (pg.get("imageinfo") or [{}])[0]
            if not ii.get("thumburl") or not str(ii.get("mime", "")).startswith("image/") or ii.get("width", 0) < 400:
                continue
            md = ii.get("extmetadata") or {}
            found.append({"title": pg.get("title", "").removeprefix("File:"), "url": ii["thumburl"], "page_url": ii.get("descriptionurl", ""),
                          "author": _strip_tags((md.get("Artist") or {}).get("value", "")) or "Unknown author",
                          "license": (md.get("LicenseShortName") or {}).get("value", "see source"),
                          "desc": _strip_tags((md.get("ImageDescription") or {}).get("value", ""))[:200]})
            if len(found) >= n:
                break
        return {"images": found}

    out: list[Candidate] = []
    res = tool("commons", search, cache_key=f"search:{n}:{query.lower()}", summary=f"commons search '{query[:40]}'")
    for i, im in enumerate(res.value_or({}).get("images", [])):
        out.append(Candidate(f"i{i}", "image", im["url"], im["title"], "Wikimedia Commons", reason=im["desc"],
                             extra={"page_url": im["page_url"], "author": im["author"], "license": im["license"]}))
    return out


def wikipedia_lead_image(page_url: str) -> Optional[Candidate]:
    """The lead image of a Wikipedia article we already linked, with its author and licence from Commons.
    Only Commons files (free licences) are used; images hosted locally on Wikipedia (fair use) are skipped."""
    p = urlparse(page_url)
    if not (p.hostname or "").endswith("wikipedia.org") or not p.path.startswith("/wiki/"):
        return None
    title = p.path.removeprefix("/wiki/")

    def summary():
        d = _json(http_get(f"https://{p.hostname}/api/rest_v1/page/summary/{title}"), "wikipedia summary")
        return {"src": ((d.get("originalimage") or d.get("thumbnail") or {}).get("source")) or ""}

    s = tool("wikipedia", summary, cache_key=f"summary:{p.hostname}:{title}", summary=f"wikipedia summary {title[:40]}")
    src = s.value_or({}).get("src", "")
    if "/wikipedia/commons/" not in src:
        return None
    segs = urlparse(src).path.split("/")
    name = unquote(segs[-2] if "/thumb/" in src else segs[-1])

    def fileinfo():
        ci = http_get("https://commons.wikimedia.org/w/api.php", params={
            "action": "query", "format": "json", "titles": f"File:{name}", "prop": "imageinfo",
            "iiprop": "url|extmetadata|mime|size", "iiurlwidth": 800})
        pages = (_json(ci, "commons file info").get("query") or {}).get("pages") or {}
        ii = (next(iter(pages.values()), {}).get("imageinfo") or [{}])[0]
        if not ii.get("thumburl"):
            raise ToolFailure("invalid", "no image info for that file", False)
        return {"ii": ii}

    f = tool("commons", fileinfo, cache_key=f"file:{name}", summary=f"commons file {name[:40]}")
    ii = f.value_or({}).get("ii") or {}
    if not ii.get("thumburl") or not str(ii.get("mime", "")).startswith("image/") or ii.get("width", 0) < 300:
        return None
    md = ii.get("extmetadata") or {}
    return Candidate("li0", "image", ii["thumburl"], name.rsplit(".", 1)[0].replace("_", " "), "Wikimedia Commons",
                     reason=_strip_tags((md.get("ImageDescription") or {}).get("value", ""))[:200],
                     extra={"page_url": ii.get("descriptionurl", ""), "author": _strip_tags((md.get("Artist") or {}).get("value", "")) or "Unknown author",
                            "license": (md.get("LicenseShortName") or {}).get("value", "see source"), "lead_of": page_url})


def discover_firecrawl(query: str, n: int = 5) -> list[Candidate]:
    """Discovery FALLBACK (used only when cheaper discovery found too little): POST https://api.firecrawl.dev/v2/search.
    Results are under data.web[]; 1 credit per result, so n is small. Through the ToolRunner: needs FIRECRAWL_API_KEY, counts against the
    per-notebook cap (FIRECRAWL_MAX_CALLS_PER_NOTEBOOK, default 20), cached, and switched off for the session after a 401 or 402."""
    def search():
        r = http_post("https://api.firecrawl.dev/v2/search", headers={"Authorization": f"Bearer {os.environ.get('FIRECRAWL_API_KEY', '')}"},
                      json={"query": query[:500], "limit": n, "sources": [{"type": "web"}]}, timeout=30)
        check_response(r, "firecrawl search")
        web = ((r.json() or {}).get("data") or {}).get("web") or []
        return {"hits": [{"url": h["url"], "title": h.get("title") or "", "description": h.get("description") or ""} for h in web if h.get("url")]}

    res = tool("firecrawl", search, cache_key=f"search:{n}:{query.lower()}", summary=f"firecrawl search '{query[:40]}'")
    return [Candidate(f"f{i}", "link", h["url"], h["title"], _domain(h["url"]), reason=snippet(h["description"]))
            for i, h in enumerate(res.value_or({}).get("hits", []))]


def discover_grounded(provider_search, model_id: str, query: str, n: int = 6) -> list[Candidate]:
    """Gemini API Google Search grounding with a Gemma 4 model (no extra key). Results are untrusted candidates."""
    try:
        hits = provider_search(model_id, f"Find the best free web pages and YouTube videos for learning: {query}")
    except Exception as e:
        log.warning("[aasaan] grounded search failed for %r: %s", query, e)
        return []
    out = []
    for i, h in enumerate(hits[:n]):
        uri = h.get("uri", "")
        vid = None
        # grounding links are redirect URLs; the final URL is resolved during verification
        out.append(Candidate(f"g{i}", "link", uri, h.get("title", ""), _domain(uri), extra={"grounded": True}))
    return out


def seed_candidates(seed_cfg: dict, text: str) -> list[Candidate]:
    low = text.lower()
    out: list[Candidate] = []
    for entry in (seed_cfg or {}).get("seeds", []):
        if any(k in low for k in entry.get("keywords", [])):
            for j, (title, url) in enumerate(entry.get("links", {}).items()):
                out.append(Candidate(f"s{len(out)}", "link", url, title, _domain(url)))
    return out


# ----------------------------------------------------------------- verification
def verify_link(c: Candidate) -> Candidate:
    """Code, not a model: the link must answer successfully with a page. Records title and a short reason."""
    ok, why = is_safe_url(c.url)
    if not ok:
        c.extra["drop"] = why
        return c
    try:
        r = http_get(c.url, timeout=TIMEOUT_S)
    except Exception as e:
        c.extra["drop"] = f"{type(e).__name__}: {e}"
        return c
    final = str(getattr(r, "url", c.url))
    ctype = r.headers.get("content-type", "")
    if r.status_code >= 400 or not ("html" in ctype or "pdf" in ctype or "text" in ctype):
        c.extra["drop"] = f"HTTP {r.status_code} {ctype}"
        return c
    if not allowed_by_robots(final):
        c.extra["drop"] = "robots.txt"
        return c
    vid = youtube_id(final)
    if vid:  # a grounded link that really is a YouTube video: verify it as a video (oEmbed), embed with the official player
        c.kind, c.extra["video_id"], c.url = "video", vid, final
        return verify_video(c)
    c.url, c.source = final, _domain(final)
    if "html" in ctype:
        html = r.text
        title = _html_title(html)
        if title and (not c.title or len(c.title) < 4 or "." in c.title.split(" ")[0] and " " not in c.title):
            c.title = title
        c.title = c.title or title or c.source
        desc = _meta_description(html)
        text = extract_text(html)
        c.reason = snippet(desc or " ".join(text.split()[:30]))
        c.extra["text"] = text[:3000]
    else:
        c.reason = c.reason or "Reference document"
    c.verified = True
    return c


def verify_video(c: Candidate) -> Candidate:
    """The video id must pass YouTube's oEmbed endpoint (exists and embeddable). Official embed only."""
    vid = c.extra.get("video_id") or youtube_id(c.url)
    if not vid:
        c.extra["drop"] = "not a YouTube watch URL"
        return c
    def oembed():
        r = http_get("https://www.youtube.com/oembed", params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}, timeout=6)
        return _json(r, "oembed")

    res = tool("oembed", oembed, cache_key=f"oembed:{vid}", summary=f"oembed {vid}")
    if not res.ok:
        c.extra["drop"] = f"oEmbed [{res.error.kind}] {res.error.message}"
        return c
    data = res.data
    c.title, c.source = data.get("title", c.title), data.get("author_name", c.source or "YouTube")
    c.extra["video_id"], c.url = vid, f"https://www.youtube.com/watch?v={vid}"
    c.verified = True
    return c


def verify_image(c: Candidate) -> Candidate:
    try:
        r = http_get(c.url, headers={"Range": "bytes=0-2047", "Accept": "image/*"}, timeout=TIMEOUT_S)
        if r.status_code in (200, 206) and r.headers.get("content-type", "").startswith("image/"):
            c.verified = True
        else:
            c.extra["drop"] = f"HTTP {r.status_code} {r.headers.get('content-type', '')}"
    except Exception as e:
        c.extra["drop"] = f"{type(e).__name__}: {e}"
    return c


def classify(c: Candidate) -> Candidate:
    """A grounded or seeded link that is really a YouTube video becomes a video candidate."""
    vid = youtube_id(c.url)
    if vid:
        c.kind, c.extra["video_id"] = "video", vid
    return c


def verify_all(cands: list[Candidate], max_workers: int = 6) -> list[Candidate]:
    from concurrent.futures import ThreadPoolExecutor
    fn = {"link": verify_link, "video": verify_video, "image": verify_image}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        done = list(pool.map(lambda c: fn[c.kind](c), cands))
    for c in done:
        if not c.verified:
            log.info("[aasaan] dropped %s %s: %s", c.kind, c.url[:80], c.extra.get("drop", ""))
    return [c for c in done if c.verified]
