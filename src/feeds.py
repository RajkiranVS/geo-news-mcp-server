'''
Feed fetching, parsing and cleaning for the geopolitical news MCP server.

Nothing in this module depends on MCP; server.py wraps fetch_feeds as a tool.
'''
import html
import logging
import re
import unicodedata
import xml.sax
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import feedparser
import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

USER_AGENT = "geopolitical-briefing-agent/0.1"
REQUEST_TIMEOUT = 10.0
SUMMARY_MAX_CHARS = 300

_TAG_RE = re.compile(r"<[^>]+>")
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200d\u2060\ufeff\u00ad]")  # zero-width space/non-joiner/joiner, word joiner, BOM, soft hyphen
_WHITESPACE_RE = re.compile(r"\s+")
# Tags are replaced with spaces, so "<b>Geneva</b>." becomes "Geneva ."; rejoin the punctuation.
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?])")
# Words dropped from keywords and item text before matching: generic news words plus
# common English function words, so e.g. "no-fly zone" doesn't match every headline with "no".
STOP_WORDS = {
    # generic news words
    "war", "wars", "news", "latest", "update", "updates", "conflict", "crisis", "situation",
    # function words
    "the", "a", "an", "and", "or", "but", "nor", "not", "no", "so", "such",
    "of", "on", "in", "into", "to", "for", "from", "by", "at", "as", "with", "about",
    "over", "after", "before", "against", "between",
    "is", "are", "was", "were", "be", "been", "being", "has", "have", "had", "do", "does", "did",
    "will", "would", "can", "could", "should", "may", "might",
    "it", "its", "this", "that", "these", "those", "he", "she", "they", "we", "you",
    "his", "her", "their", "our", "your", "what", "which", "when", "where", "how", "why",
    "all", "any", "some", "more", "most", "very", "than", "then", "there", "here",
}

# Words that count as the same place: a country's name, short forms and adjective
# forms. A keyword word found in any of these sets matches every word in that set,
# so "Iran" matches "Iranian(s)" and "UK" matches "Britain". Only countries that
# actually appear in the feeds are listed.
EQUIVALENT_WORDS: list[set[str]] = [
    {"us", "usa", "america", "american", "americans"},
    {"uk", "britain", "british"},
    {"uae", "emirates"},
    {"saudi", "ksa", "arabia"},  # "arabia" so "Saudi Arabia" is one group, not two
    {"iran", "iranian", "iranians"},
    {"israel", "israeli", "israelis"},
    {"india", "indian", "indians"},
    {"pakistan", "pakistani", "pakistanis"},
    {"china", "chinese"},
    {"russia", "russian", "russians"},
    {"ukraine", "ukrainian", "ukrainians"},
    {"syria", "syrian", "syrians"},
    {"yemen", "yemeni"},
    {"korea", "korean", "koreans"},
]
_EQUIVALENTS: dict[str, frozenset[str]] = {w: frozenset(group) for group in EQUIVALENT_WORDS for w in group}

# Keyword words that stand for other words. Each entry is a list of groups that
# must ALL be found; within a group, any word will do (after EQUIVALENT_WORDS is
# applied). Multi-word names are split into separate groups because item text is
# tokenized into single words: "hk" requires both "hong" and "kong".
KEYWORD_ALIASES: dict[str, list[set[str]]] = {
    "indo": [{"india"}],
    "pak": [{"pakistan"}],
    "indopak": [{"india"}, {"pakistan"}],
    "prc": [{"china"}],
    "dprk": [{"north"}, {"korea"}],  # both, so South Korea stories don't match
    "hk": [{"hong"}, {"kong"}],
    "sg": [{"singapore"}],
    "ph": [{"philippines"}],
}

FeedName = Literal["bbc_world", "hindu_international", "hindu_national"]
FeedURLs: dict[FeedName, str] = {
    "bbc_world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "hindu_international": "https://www.thehindu.com/news/international/feeder/default.rss",
    "hindu_national": "https://www.thehindu.com/news/national/feeder/default.rss",
}


class FeedItem(BaseModel):
    '''
    A model representing a feed item.
    '''
    source: FeedName
    title: str
    link: str
    published_utc: datetime | None
    summary: str


class FeedError(BaseModel):
    '''
    A feed that could not be fetched or parsed, or was only partly readable.
    feed is the source that failed.
    reason is one of: "timeout", "http_<status>", "connection_error", "malformed_feed",
    "incomplete_feed", "unknown_error".
    detail is a short, human-readable explanation of the reason.

    "incomplete_feed" is the one reason where the feed still contributed items: its
    XML broke partway through, so the items read before the break are returned but
    some may be missing.
    '''
    feed: str
    reason: str
    detail: str


class FeedResult(BaseModel):
    '''
    News items from the feeds that succeeded, plus the feeds that failed.
    '''
    items: list[FeedItem]
    failed_feeds: list[FeedError]


class MalformedFeedError(Exception):
    '''
    Raised when a feed responds successfully but its body cannot be parsed as RSS/Atom.
    Caught per feed in fetch_feeds and reported in FeedResult.failed_feeds.
    '''

    def __init__(self, feed_name: str, url: str, detail: object | None = None):
        self.feed_name = feed_name
        self.url = url
        message = f"Malformed feed '{feed_name}' ({url}): the response could not be parsed as RSS/Atom."
        if detail:
            message += f" Parser said: {detail}"
        super().__init__(message)


def _clean_text(text: str) -> str:
    '''Unescape entities, strip HTML, NFKC-normalise, drop zero-width characters, collapse whitespace and remove spaces before punctuation.'''
    # Unescape before stripping tags: feeds may send escaped markup such as
    # "&lt;p&gt;Text&lt;/p&gt;", which only becomes a strippable tag once unescaped.
    text = html.unescape(text)
    text = _TAG_RE.sub(" ", text)
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    return text.strip()

def _tokens(text: str) -> list[str]:
    '''
    Split text into lowercase word tokens for keyword matching.

    Periods are removed first so "U.S." becomes "us"; punctuation, hyphens and dashes
    split words; stop words and one-letter tokens are dropped. Order is preserved and
    duplicates are kept.

    Args:
        text: Any text — a user keyword or an item's title and summary.
    Returns:
        List of tokens.
    '''
    cleaned = _clean_text(text).casefold().replace(".", "")
    return [word for word in re.findall(r"\w+", cleaned) if word not in STOP_WORDS and len(word) >= 2]


def _expand(words: set[str]) -> frozenset[str]:
    '''Add every equivalent word (see EQUIVALENT_WORDS) to a group.'''
    return frozenset().union(*(_EQUIVALENTS.get(w, {w}) for w in words))


def _keyword_terms(keyword: str | None) -> list[frozenset[str]]:
    '''
    Turn a user keyword into match groups. An item matches when it contains at least
    one word from every group.

    Each token from _tokens(keyword) becomes the groups in KEYWORD_ALIASES, or a
    single-word group if it has no alias; each group is then widened with
    EQUIVALENT_WORDS. Duplicate groups are removed, order kept. Returns [] for None
    or an empty/stop-word-only keyword (meaning: no filtering).

    Args:
        keyword: The user's search keyword, or None.
    Returns:
        Match groups in first-seen order; [] if there are none.
    '''
    if not keyword:
        return []
    groups: list[frozenset[str]] = []
    for token in _tokens(keyword):
        for group in KEYWORD_ALIASES.get(token, [{token}]):
            expanded = _expand(group)
            if expanded not in groups:
                groups.append(expanded)
    return groups


def _matches_all(groups: list[frozenset[str]], text: str) -> bool:
    '''True if text contains at least one word from every group.'''
    tokens = set(_tokens(text))
    return all(group & tokens for group in groups)


def _truncate(text: str, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    '''Cut text to at most max_chars at a word boundary, adding "…" if anything was cut.'''
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1]
    # Back up to the last space unless the cut already falls on a word boundary.
    if not text[max_chars - 1].isspace() and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.-") + "…"


def _strip_query(url: str) -> str:
    '''
    Remove the whole query string (e.g. tracking parameters) from a URL.

    Safe for the current sources (BBC, The Hindu): their article URLs are
    path-based and the query string only carries tracking parameters.
    If you add a feed whose article identity lives in the query (e.g. "?id=123"),
    this would break its links: change it to drop only known tracking
    parameters (utm_*, at_*) and keep everything else.
    '''
    parts = urlsplit(url.strip())
    return urlunsplit(parts._replace(query=""))


INCOMPLETE_FEED_DETAIL = "The source's feed was incomplete; some items may be missing."


def _fetch_one(client: httpx.Client, feed_name: FeedName, url: str) -> tuple[list[FeedItem], bool]:
    '''
    Fetch and parse a single feed. Returns (items, incomplete).
    Raises httpx.HTTPError or MalformedFeedError.

    Checks, in order:
      1. HTTP status (raise_for_status).
      2. No feed version: the body is not RSS/Atom at all (e.g. an HTML error page
         served with 200), so it is malformed however many "entries" it has.
      3. A real XML parse error (xml.sax.SAXParseException): malformed if nothing was
         read, otherwise keep the items read before the break and flag incomplete.
         Other bozo exceptions (CharacterEncodingOverride, NonXMLContentType, ...)
         are harmless warnings and are ignored.
    A recognised feed with no items is a valid, empty feed: "no news", not an error.

    Summaries are cleaned but NOT truncated, so keyword filtering sees the full text;
    fetch_feeds truncates only the items it returns.
    '''
    response = client.get(url)
    response.raise_for_status()
    parsed_feed = feedparser.parse(response.text)
    bozo_exception = parsed_feed.get("bozo_exception")

    if not parsed_feed.get("version"):
        raise MalformedFeedError(feed_name, url, bozo_exception or "not a recognised RSS/Atom feed")

    xml_broken = isinstance(bozo_exception, xml.sax.SAXParseException)
    if xml_broken and not parsed_feed.entries:
        # feedparser still reports a version when the XML breaks after the <rss> tag,
        # so "version set, no entries" alone does not mean a valid empty feed.
        raise MalformedFeedError(feed_name, url, bozo_exception)

    items: list[FeedItem] = []
    for entry in parsed_feed.entries:
        published_dt = entry.get("published_parsed")
        items.append(
            FeedItem(
                source=feed_name,
                title=_clean_text(entry.get("title", "")),
                link=_strip_query(entry.get("link", "")),
                published_utc=datetime(*published_dt[:6], tzinfo=timezone.utc) if published_dt else None,
                summary=_clean_text(entry.get("summary", "")),
            )
        )
    return items, xml_broken


def _describe_failure(exc: Exception) -> tuple[str, str]:
    '''Map an exception to a (reason code, short fixed message) safe to show the agent.'''
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", "The source did not respond in time."
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return f"http_{status}", f"The source returned HTTP {status}."
    if isinstance(exc, httpx.HTTPError):
        return "connection_error", "Could not connect to the source."
    if isinstance(exc, MalformedFeedError):
        return "malformed_feed", "The source's response could not be read as a news feed."
    return "unknown_error", "An unexpected error occurred while processing the source."


def fetch_feeds(feeds: list[FeedName], keyword: str | None = None, limit: int = 5) -> FeedResult:
    '''
    Fetch each feed independently, rank items by keyword relevance and return
    at most `limit` items with truncated summaries.

    The keyword is turned into match groups (see _keyword_terms). An item is kept
    only if its title or summary contains at least one word from every group.
    Results are sorted newest first (undated items last). If the keyword is missing or contains only stop words, every item is
    kept, newest first. A failing feed is reported in failed_feeds instead of
    stopping the others.
    '''
    items: list[FeedItem] = []
    failed: list[FeedError] = []

    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for feed_name in dict.fromkeys(feeds):
            url = FeedURLs[feed_name]
            try:
                feed_items, incomplete = _fetch_one(client, feed_name, url)
                items.extend(feed_items)
                if incomplete:
                    logger.warning(
                        "Feed %s (%s) is incomplete: XML broke after %d items", feed_name, url, len(feed_items)
                    )
                    failed.append(FeedError(feed=feed_name, reason="incomplete_feed", detail=INCOMPLETE_FEED_DETAIL))
            except (httpx.HTTPError, MalformedFeedError) as exc:
                reason, detail = _describe_failure(exc)
                logger.warning("Feed %s (%s) failed: %s", feed_name, url, reason, exc_info=exc)
                failed.append(FeedError(feed=feed_name, reason=reason, detail=detail))
            except Exception as exc:
                reason, detail = _describe_failure(exc)
                logger.exception("Unexpected error processing feed %s (%s)", feed_name, url)
                failed.append(FeedError(feed=feed_name, reason=reason, detail=detail))

    groups = _keyword_terms(keyword)
    if groups:
        items = [item for item in items if _matches_all(groups, f"{item.title} {item.summary}")]

    items.sort(key=lambda item: -item.published_utc.timestamp() if item.published_utc is not None else float("inf"))

    # Truncate only after ranking, and only the items actually returned.
    results = [item.model_copy(update={"summary": _truncate(item.summary)}) for item in items[:limit]]

    return FeedResult(items=results, failed_feeds=failed)
