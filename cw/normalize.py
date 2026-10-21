"""Turn raw HTML / XML into stable text and lists. Deterministic: same input, same output."""

from __future__ import annotations

import difflib
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

NORM_TEXT_CAP = 40_000          # chars kept; hashed after the cap so hash and text agree
DIFF_CAP_LINES = 80

DROP_TAGS = {"script", "style", "noscript", "svg", "iframe", "template"}
# Elements whose id/class/role smell of page chrome. Never applied to <html>/<body>:
# their class lists are theme flags (a body class like `mfn-header-template` would
# empty the whole page).
CRUFT_ATTR_RE = re.compile(r"(cookie|consent|gdpr|banner|nav|footer|header|menu|popup|modal"
                           r"|skip-link|skip-to|sr-only|visually-hidden)", re.I)
BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "tr", "td", "th", "section", "article", "aside",
              "main", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
              "figure", "figcaption", "form", "label", "option", "dt", "dd", "table"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "param", "source", "track", "wbr"}
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
DATE_TOKEN_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|(?:%s)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}\s+(?:%s)[a-z]*\.?\s+\d{4})\b" % (_MONTHS, _MONTHS), re.I)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self.title = ""
        self._stack: list = []
        self._suppress = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        drop = tag in DROP_TAGS or (tag not in ("html", "body") and bool(CRUFT_ATTR_RE.search(
            " ".join(filter(None, [a.get("id"), a.get("class"), a.get("role"),
                                   a.get("aria-label"), a.get("data-testid")])))))
        if tag == "title":
            self._in_title = True
        if tag in VOID_TAGS:
            if tag == "br" and not self._suppress:
                self.parts.append("\n")
            return
        self._stack.append((tag, drop))
        if drop:
            self._suppress += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and not self._suppress:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                for _, dropped in self._stack[i:]:
                    if dropped:
                        self._suppress -= 1
                del self._stack[i:]
                break
        if tag in BLOCK_TAGS and not self._suppress:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            if not self._suppress:
                self.title += data
            return
        if not self._suppress:
            self.parts.append(data)


def normalize_page(html: str) -> tuple:
    """→ (title, text). Visible text only, one line per block, with dates, digit-only
    lines and fragments under three words removed, so a footer year or a cookie banner
    never reads as a page change."""
    p = _TextExtractor()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    out = []
    for line in "".join(p.parts).splitlines():
        line = DATE_TOKEN_RE.sub("", line)
        line = re.sub(r"[ \t\xa0]+", " ", line).strip()
        if not line or re.fullmatch(r"[\d\s.,:%$€£+\-/()]+", line) or len(line.split()) < 3:
            continue
        out.append(line)
    return re.sub(r"\s+", " ", p.title).strip(), "\n".join(out)[:NORM_TEXT_CAP]


def unified_diff(prev: str, cur: str) -> str:
    d = list(difflib.unified_diff(prev.splitlines(), cur.splitlines(),
                                  fromfile="prev", tofile="cur", lineterm="", n=1))
    if len(d) > DIFF_CAP_LINES:
        d = d[:DIFF_CAP_LINES] + [f"… diff truncated at {DIFF_CAP_LINES} lines"]
    return "\n".join(d)


def diff_stats(diff: str) -> tuple:
    """→ (changed_lines, changed_chars, added_lines)."""
    added, n, chars = [], 0, 0
    for ln in (diff or "").splitlines():
        if not ln or ln.startswith(("+++", "---", "@@", "…")):
            continue
        if ln[0] in "+-":
            n += 1
            chars += len(ln) - 1
            if ln[0] == "+":
                added.append(ln[1:].strip())
    return n, chars, added


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def parse_sitemap(xml_bytes: bytes) -> tuple:
    """→ (urls, child_sitemaps, lastmod_by_url, error). A parse failure is an error,
    never an empty set: an unreported failure reads as "they deleted every page"."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        return [], [], {}, f"sitemap is not XML ({e})"
    kind = _strip_ns(root.tag)
    urls, children, lastmod = [], [], {}
    for el in root:
        if _strip_ns(el.tag) not in ("url", "sitemap"):
            continue
        loc = next((c.text for c in el if _strip_ns(c.tag) == "loc" and c.text), None)
        if not loc:
            continue
        loc = loc.strip()
        if kind == "sitemapindex":
            children.append(loc)
            continue
        urls.append(loc)
        lm = next((c.text for c in el if _strip_ns(c.tag) == "lastmod" and c.text), None)
        if lm:
            lastmod[loc] = lm.strip()
    return urls, children, lastmod, ""


def parse_rss(xml_bytes: bytes) -> tuple:
    """→ (items, error)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        return [], f"RSS is not XML ({e})"
    items = []
    for it in root.iter():
        if _strip_ns(it.tag) != "item":
            continue

        def get(name):
            return next((c.text or "" for c in it if _strip_ns(c.tag) == name), "")

        src = next((c for c in it if _strip_ns(c.tag) == "source"), None)
        items.append({
            "title": re.sub(r"\s+", " ", get("title")).strip(),
            "url": get("link").strip(),
            "published_at": get("pubDate").strip(),
            "source": (src.text or "").strip() if src is not None else "",
            "snippet": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", get("description")))[:400].strip(),
            "provider": "google_news",
        })
    return items, ""
