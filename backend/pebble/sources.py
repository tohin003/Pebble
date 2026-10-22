"""Explicitly attached sources are untrusted data, with no tool authority.

Reading is bounded at every step. Only public https:// URLs are fetched, and each
redirect hop (at most three) is validated and resolved again, so a chain never
reaches a private address. A fetch keeps at most 2 MB; any excess is dropped and
the prefix is flagged. Only HTML, text and PDF are read, PDFs through the confined
document parser. The declared charset is honoured. Hidden text is counted, never
kept, and the page's own date claims stay untrusted metadata with their basis.
"""

import codecs
import datetime
import email.utils
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
import time
import certifi
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

MAX_FETCH_BYTES = 2_000_000  # per fetch; the excess is dropped and the prefix flagged
MAX_SOURCE_CHARS = 200_000  # extracted text kept per source
MAX_REDIRECTS = 3
LEGACY_BYTES = 192_000  # fetch_public: search and weather responses must be whole
LEGACY_CHARS = 18_000  # fetch_url: attached source text
REDIRECTS = frozenset({301, 302, 303, 307, 308})
READ_ACCEPT = "text/html,application/xhtml+xml,text/plain;q=0.9,application/pdf;q=0.8,text/*;q=0.5"
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
PDF_TYPES = frozenset({"application/pdf", "application/x-pdf"})
TEXT_TYPES = frozenset({"application/json", "application/ld+json", "application/xml",
                        "application/rss+xml", "application/atom+xml"})
BINARY_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})
UNSUPPORTED = "Only HTML, text and PDF web sources can be read."
EARLIEST, LATEST = -2208988800, 4102444800  # 1900-01-01 and 2100-01-01, UTC

SKIP = frozenset({"script", "style", "noscript", "svg", "iframe", "object"})
VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
BLOCKS = frozenset(("address article aside blockquote br dd details div dl dt fieldset figcaption figure "
                    "footer form h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section summary table "
                    "td th tr ul").split())
CLOSES_P = frozenset(("address article aside blockquote details div dl fieldset figcaption figure footer "
                      "form h1 h2 h3 h4 h5 h6 header hr main menu nav ol p pre section table ul").split())
IMPLIED = {"li": ("li",), "dt": ("dt", "dd"), "dd": ("dt", "dd"), "option": ("option",),
           "tr": ("td", "th", "tr"), "td": ("td", "th"), "th": ("td", "th")}
STYLE_HIDDEN = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)"
    r"|opacity\s*:\s*0*(?:\.0+)?\s*(?:!important)?\s*(?:;|$)"
    r"|font-size\s*:\s*0+(?:\.0+)?(?:px|em|rem|pt|%)?\s*(?:!important)?\s*(?:;|$)"
    r"|(?:left|top|text-indent)\s*:\s*-\d{4,})", re.I)
# Page-claimed dates from <meta property|name|itemprop>; untrusted, shown with their basis.
META_DATES = {
    "article:published_time": "published", "datepublished": "published", "date": "published",
    "dc.date": "published", "dc.date.issued": "published", "dcterms.date": "published",
    "dcterms.issued": "published", "dcterms.created": "published", "citation_publication_date": "published",
    "citation_date": "published", "citation_online_date": "published", "pubdate": "published",
    "publishdate": "published", "publish-date": "published", "parsely-pub-date": "published",
    "article:modified_time": "modified", "og:updated_time": "modified", "datemodified": "modified",
    "dcterms.modified": "modified", "dc.date.modified": "modified", "last-modified": "modified",
    "lastmod": "modified",
}
LD_DATES = (("datePublished", "published"), ("dateCreated", "published"), ("dateModified", "modified"))
META_CHARSET = re.compile(rb"""<meta[^>]{0,200}?charset\s*=\s*["']?\s*([A-Za-z0-9._:-]{1,40})""", re.I)
CHARSETS = frozenset(codecs.lookup(name).name for name in (
    "utf-8", "utf-16-le", "utf-16-be", "cp1250", "cp1251", "cp1252", "cp1253", "cp1254", "cp1255",
    "cp1256", "cp1257", "cp1258", "cp874", "iso-8859-2", "iso-8859-3", "iso-8859-4", "iso-8859-5",
    "iso-8859-6", "iso-8859-7", "iso-8859-8", "iso-8859-9", "iso-8859-10", "iso-8859-13", "iso-8859-14",
    "iso-8859-15", "iso-8859-16", "koi8-r", "koi8-u", "mac-roman", "shift_jis", "cp932", "euc-jp",
    "iso-2022-jp", "gb2312", "gbk", "gb18030", "big5", "big5hkscs", "euc-kr", "cp949"))
# WHATWG Encoding: these labels mean windows-1252, and a bare "utf-16" label means little-endian.
WHATWG = {codecs.lookup("latin-1").name: "cp1252", "ascii": "cp1252", "utf-16": "utf-16-le"}
# Bidi overrides, zero-width spaces, word joiners, BOMs and soft hyphens hide or reorder text.
INVISIBLE = re.compile("[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")  # T7 F1: also ALM, MVS, ZWNJ/ZWJ, LRM/RLM and the tag block
CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
SURROGATE = re.compile("[\ud800-\udfff]")


def concealed(attrs):
    """Whether an element's own attributes hide it from a reader."""
    return ("hidden" in attrs or attrs.get("aria-hidden", "").strip().lower() == "true"
            or attrs.get("type", "").strip().lower() == "hidden"
            or bool(STYLE_HIDDEN.search(attrs.get("style", ""))))


class TextHTML(HTMLParser):
    """Visible text of an untrusted page. Hidden text is counted, never kept."""

    def __init__(self):
        super().__init__()
        self.skip = 0
        self.hidden = 0
        self.hidden_chars = 0
        self.parts = []
        self.title = []
        self.stack = []  # (tag, 'skip' | 'hidden' | None) for each open element
        self.open = {}  # tag -> open count, so an unmatched end tag costs nothing
        self.meta = {}  # 'published' / 'modified' -> raw claimed date
        self.times = []
        self.ld = []
        self.ld_chars = 0
        self.capture = None

    def visible(self):
        return not (self.skip or self.hidden)

    def handle_starttag(self, tag, attrs):
        attrs = {k: v or "" for k, v in attrs}
        if tag == "meta":
            self._meta(attrs)
        if tag in VOID:
            if tag in ("br", "hr") and self.visible():
                self.parts.append("\n")
            return
        while self.stack and (self.stack[-1][0] in IMPLIED.get(tag, ())
                              or tag in CLOSES_P and self.stack[-1][0] == "p"):
            self._pop()
        kind = "skip" if tag in SKIP else "hidden" if tag == "template" or concealed(attrs) else None
        if tag == "time" and kind is None and self.visible() and attrs.get("datetime") and len(self.times) < 8:
            self.times.append(attrs["datetime"][:64])
        if (tag == "script" and attrs.get("type", "").strip().lower() == "application/ld+json"
                and not self.hidden and len(self.ld) < 4):
            self.capture = []
        self.stack.append((tag, kind))
        self.open[tag] = self.open.get(tag, 0) + 1
        if kind == "skip":
            self.skip += 1
        elif kind == "hidden":
            self.hidden += 1
        if tag in BLOCKS and self.visible():
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in VOID or not self.open.get(tag):
            return
        while self.stack and self._pop() != tag:
            pass
        if tag in BLOCKS and self.visible():
            self.parts.append("\n")

    def _pop(self):
        tag, kind = self.stack.pop()
        self.open[tag] -= 1
        if kind == "skip":
            self.skip -= 1
        elif kind == "hidden":
            self.hidden -= 1
        if tag == "script" and self.capture is not None:
            self.ld.append("".join(self.capture))
            self.capture = None
        return tag

    def _meta(self, attrs):
        if not self.visible():
            return
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").strip().lower()
        kind, value = META_DATES.get(key), attrs.get("content") or attrs.get("datetime")
        if kind and value and kind not in self.meta:
            self.meta[kind] = value[:64]

    def handle_data(self, data):
        if self.capture is not None:
            room = 65536 - self.ld_chars
            if room > 0:
                self.capture.append(data[:room])
                self.ld_chars += min(room, len(data))
            return
        if self.skip:
            return
        if self.hidden:
            self.hidden_chars += len(data.strip())
            return
        if self.stack and self.stack[-1][0] == "title":
            self.title.append(data)
        self.parts.append(data)


def public_addresses(host):
    addresses = list(
        dict.fromkeys(
            r[4][0] for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        )
    )
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError(
            "Only public HTTPS sources are allowed. Local and private networks are blocked."
        )
    return addresses


def check_url(url):
    """The split URL when it is an https:// URL without credentials, a custom port or controls."""
    if not isinstance(url, str) or len(url) > 3000:
        raise ValueError("Use a public URL shorter than 3,000 characters.")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or re.search(r"[\x00-\x20\x7f]", url)
    ):
        raise ValueError(
            "Enter a public https:// URL without credentials or a custom port."
        )
    return parsed


def _get(parsed, accept, deadline, max_bytes, prefix_ok):
    """One validated hop: a redirect's Location, or a bounded 200 body."""
    addresses = public_addresses(parsed.hostname)
    context = ssl.create_default_context(cafile=certifi.where())
    failure = "Could not reach this web source. Check your connection or try again."
    for ip in addresses[:2]:

        class PinnedHTTPS(http.client.HTTPSConnection):
            def connect(self):
                raw = socket.create_connection((ip, 443), timeout=6)
                self.sock = context.wrap_socket(raw, server_hostname=self.host)

        conn = PinnedHTTPS(parsed.hostname, timeout=6, context=context)
        try:
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            conn.request(
                "GET",
                target,
                headers={
                    "User-Agent": "Pebble/0.2 (local source reader)",
                    "Accept": accept,
                },
            )
            res = conn.getresponse()
            if 300 <= res.status < 400:
                return {"status": res.status, "location": res.getheader("Location", "") or ""}
            if res.status != 200:
                if res.status in {429, 500, 502, 503, 504}:
                    failure = f"The web source is temporarily unavailable (HTTP {res.status}). Try again shortly."
                    continue
                raise ValueError(f"Source returned HTTP {res.status}.")
            chunks = []
            size = 0
            truncated = False
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("Web source exceeded its time budget.")
                chunk = res.read1(8192)
                if not chunk:
                    break
                if size + len(chunk) > max_bytes:
                    if not prefix_ok:
                        raise ValueError(f"Web source exceeds the {max_bytes // 1000:,} KB reading limit.")
                    chunks.append(chunk[: max_bytes - size])
                    size, truncated = max_bytes, True
                    break
                size += len(chunk)
                chunks.append(chunk)
            return {
                "status": 200,
                "body": b"".join(chunks),
                "truncated": truncated,
                "content_type": res.getheader("Content-Type", "") or "",
                "date": res.getheader("Date"),
                "last_modified": res.getheader("Last-Modified"),
            }
        except (OSError, ssl.SSLError):
            if time.monotonic() > deadline:
                break
        finally:
            conn.close()
    raise ValueError(failure)


def fetch_response(url, accept=READ_ACCEPT, *, max_bytes=MAX_FETCH_BYTES, max_redirects=MAX_REDIRECTS,
                   prefix_ok=True, seconds=18):
    """GET a public https:// URL. Every redirect hop is validated and resolved again before it is
    contacted. Returns url, final_url, redirects, content_type, date, last_modified, body, bytes and
    truncated (the body is a prefix of at most max_bytes)."""
    deadline = time.monotonic() + seconds
    current, redirects = url, []
    seen = {url.split("#", 1)[0]} if isinstance(url, str) else set()
    while True:
        hop = _get(check_url(current), accept, deadline, max_bytes, prefix_ok)
        if hop["status"] == 200:
            break
        if max_redirects == 0:
            raise ValueError("This URL redirects. Attach the final destination URL explicitly.")
        if hop["status"] not in REDIRECTS or not hop["location"].strip():
            raise ValueError("This URL redirects in a way Pebble does not follow. Read the destination URL directly.")
        if len(redirects) >= max_redirects:
            raise ValueError(f"This URL redirected more than {max_redirects} times. Read the final destination URL directly.")
        target = urljoin(current, hop["location"].strip())
        try:
            check_url(target)
        except ValueError:
            raise ValueError("This URL redirects to an address Pebble does not read. "
                             "Only public https:// destinations are followed.") from None
        if target.split("#", 1)[0] in seen:
            raise ValueError("This URL redirects in a loop.")
        seen.add(target.split("#", 1)[0])
        redirects.append(target)
        current = target
    hop.update(url=url, final_url=current, redirects=redirects, bytes=len(hop["body"]))
    return hop


def fetch_public(url, accept="text/html,text/plain"):
    """A whole response from a public https:// URL that does not redirect (search, weather)."""
    response = fetch_response(url, accept, max_bytes=LEGACY_BYTES, max_redirects=0, prefix_ok=False)
    return response["body"], response["content_type"]


def parse_date(value):
    """Epoch seconds for an HTTP, RFC 2822 or ISO 8601 date (naive means UTC), or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()[:64]
    try:
        if re.match(r"\d{4}-\d{2}-\d{2}", text):
            try:
                when = datetime.datetime.fromisoformat(text)
            except ValueError:
                when = datetime.datetime.fromisoformat(text[:10])
        else:
            when = email.utils.parsedate_to_datetime(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        stamp = when.timestamp()
    except (TypeError, ValueError, IndexError, OverflowError, OSError):
        return None
    return stamp if EARLIEST <= stamp <= LATEST else None


def iso_date(stamp):
    """UTC ISO 8601 text for epoch seconds, or None."""
    if stamp is None:
        return None
    return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def media_type(value):
    """('type/subtype', declared charset or None) from a Content-Type header."""
    base, _, params = str(value or "")[:300].partition(";")
    match = re.search(r"""charset\s*=\s*["']?([A-Za-z0-9._:-]{1,40})""", params, re.I)
    return base.strip().lower(), match.group(1) if match else None


def _decode(data, name, final, errors="replace"):
    return codecs.getincrementaldecoder(name)(errors).decode(data, final)


def decode(body, declared=None, *, html=False, final=True):
    """(text, encoding, flags): a BOM, then the declared or <meta> charset, then UTF-8, then
    windows-1252. A prefix (final=False) drops a cut-off trailing character."""
    for bom, name in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"),
                      (codecs.BOM_UTF16_BE, "utf-16-be")):
        if body.startswith(bom):
            return _decode(body[len(bom):], name, final), name, []
    flags = []
    if not declared and html:
        match = META_CHARSET.search(body[:4096])
        declared = match.group(1).decode("ascii") if match else None
    if declared:
        try:
            name = codecs.lookup(declared).name
        except LookupError:
            name = None
        name = WHATWG.get(name, name)
        if name in CHARSETS:
            text = _decode(body, name, final)
            return text, name, ["replacement_characters"] if "�" in text else []
        flags.append("charset_unsupported")
    try:
        return _decode(body, "utf-8", final, "strict"), "utf-8", flags
    except UnicodeDecodeError:
        return _decode(body, "cp1252", final), "cp1252", flags + ["charset_guessed"]


def clean(text, collapse=False):
    """(visible lines, invisible characters removed): controls and bidi/zero-width format
    characters removed, blank lines dropped; HTML whitespace runs collapse."""
    text = SURROGATE.sub("�", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u2028", "\n").replace("\u2029", "\n")
    stripped = INVISIBLE.sub("", text)
    removed = len(text) - len(stripped)
    text = CONTROL.sub(" ", stripped)
    lines = (" ".join(line.split()) if collapse else line.strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line), removed


def ld_dates(blobs):
    """Claimed dates from JSON-LD blocks, bounded in depth and nodes."""
    found = {}
    for blob in blobs:
        try:
            data = json.loads(blob)
        except (ValueError, RecursionError):
            continue
        stack, seen = [(data, 0)], 0
        while stack and seen < 500:
            item, depth = stack.pop()
            seen += 1
            if isinstance(item, dict):
                for key, kind in LD_DATES:
                    value = item.get(key)
                    if isinstance(value, str) and kind not in found:
                        found[kind] = value[:64]
                children = list(item.values())
            else:
                children = item[:100]
            if depth < 6:
                stack.extend((child, depth + 1) for child in reversed(children)
                             if isinstance(child, (dict, list)))
    return found


def pdf_text(body, parser=None):
    """(text, flags) from complete PDF bytes, through the confined document parser and its limits."""
    if not body.startswith(b"%PDF-"):
        raise ValueError("This web source is labelled as a PDF but is not one.")
    if parser is None:
        from .documents import parse_document as parser
    with tempfile.TemporaryDirectory(prefix="pebble-source-") as folder:
        path = Path(folder) / "source.pdf"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
        output = parser(path, "--extract-file")
    try:
        parsed = json.loads(output)
        text, notice = parsed.get("text"), parsed.get("notice") or ""
    except (TypeError, ValueError, AttributeError):
        text = notice = None
    if not isinstance(text, str) or not isinstance(notice, str):
        raise ValueError("Could not read this PDF.")
    flags = []
    if notice.startswith("PDF text was truncated"):
        flags.append("pdf_text_truncated")
    if notice.startswith("The first 200 PDF pages"):
        flags.append("pdf_pages_truncated")
    if "no readable text" in notice:
        flags.append("pdf_no_text")
    return text, flags


def _claimed(parser):
    """(published raw, modified raw, basis) from <meta>, then JSON-LD, then <time datetime>."""
    chosen, bases = {}, []
    for basis, values in (("meta", parser.meta), ("json_ld", ld_dates(parser.ld)),
                          ("time_element", {"published": parser.times[0]} if parser.times else {})):
        for kind in ("published", "modified"):
            stamp = parse_date(values.get(kind))
            if kind not in chosen and stamp is not None:
                chosen[kind] = stamp
                if basis not in bases:
                    bases.append(basis)
    return chosen.get("published"), chosen.get("modified"), "+".join(bases) or "none"


def extract_document(response, *, max_chars=MAX_SOURCE_CHARS, pdf_parser=None):
    """Untrusted text and metadata from a fetch_response() result.

    Returns url, final_url, redirects, kind ('page' or 'pdf'), content_type, encoding, title,
    text, characters, bytes, body_sha256, truncated, http_date, last_modified, claimed_published,
    claimed_modified (epoch seconds or None), date_basis, hidden_text_removed and flags."""
    body = response["body"]
    truncated = bool(response.get("truncated"))
    flags = ["fetch_bytes_cap"] if truncated else []
    content_type, declared = media_type(response.get("content_type"))
    title, hidden, encoding = "", 0, None
    published = modified = None
    basis = "none"
    head = body[:512].lstrip().lower()
    if content_type in PDF_TYPES or content_type in BINARY_TYPES and body.startswith(b"%PDF-"):
        if truncated:
            raise ValueError(f"This PDF is larger than this read's {len(body):,}-byte limit. "
                             "Only complete PDFs can be read.")
        raw, more = pdf_text(body, pdf_parser)
        flags += more
        truncated = truncated or "pdf_text_truncated" in more or "pdf_pages_truncated" in more
        text, invisible = clean(raw)
        kind, content_type = "pdf", "application/pdf"
    elif content_type in HTML_TYPES or content_type == "" and head.startswith((b"<!doctype html", b"<html")):
        raw, encoding, more = decode(body, declared, html=True, final=not truncated)
        flags += more
        parser = TextHTML()
        parser.feed(raw)
        parser.close()
        text, invisible = clean("".join(parser.parts), collapse=True)
        title = clean("".join(parser.title), collapse=True)[0].replace("\n", " ")[:180]
        hidden = parser.hidden_chars
        published, modified, basis = _claimed(parser)
        kind, content_type = "page", content_type or "text/html"
    elif content_type.startswith("text/") or content_type in TEXT_TYPES:
        raw, encoding, more = decode(body, declared, final=not truncated)
        flags += more
        text, invisible = clean(raw)
        kind = "page"
    else:
        raise ValueError(UNSUPPORTED)
    if invisible:
        flags.append("invisible_characters_removed")
    if len(text) > max_chars:
        text, truncated = text[:max_chars], True
        flags.append("source_chars_cap")
    return {
        "url": response.get("url"),
        "final_url": response.get("final_url") or response.get("url"),
        "redirects": list(response.get("redirects") or []),
        "kind": kind,
        "content_type": content_type,
        "encoding": encoding,
        "title": title,
        "text": text,
        "characters": len(text),
        "bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "truncated": truncated,
        "http_date": parse_date(response.get("date")),
        "last_modified": parse_date(response.get("last_modified")),
        "claimed_published": published,
        "claimed_modified": modified,
        "date_basis": basis,
        "hidden_text_removed": hidden,
        "flags": flags,
    }


def fetch_document(url, *, max_bytes=MAX_FETCH_BYTES, max_chars=MAX_SOURCE_CHARS, pdf_parser=None):
    """fetch_response() then extract_document(): the reader behind the source ledger."""
    return extract_document(fetch_response(url, READ_ACCEPT, max_bytes=max_bytes),
                            max_chars=max_chars, pdf_parser=pdf_parser)


def fetch_url(url):
    """Attached-source text: at most 18,000 characters, with a visible note when shortened."""
    document = fetch_document(url)
    text = document["text"]
    if len(text) > LEGACY_CHARS or document["truncated"]:
        text = text[:LEGACY_CHARS] + "\n[Pebble: only the beginning of this source is attached.]"
    return text
