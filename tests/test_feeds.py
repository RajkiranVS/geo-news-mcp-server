'''
Offline tests for feeds.fetch_feeds.

Real-feed behaviour uses the saved responses in tests/fixtures/ (captured
2026-11-10). Cases the real feeds don't currently show (HTML in summaries,
undated items) use small inline RSS documents.
'''
from datetime import datetime, timezone

import feedparser
import httpx
import pytest

from feeds import FeedError, FeedURLs, _keyword_terms, fetch_feeds
from tests.conftest import load_fixture, rss_response

ALL_FEEDS = ["bbc_world", "hindu_international", "hindu_national"]


def rss(*items: str) -> str:
    '''Wrap <item> fragments in a minimal RSS 2.0 document.'''
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Test</title><link>https://example.com/</link>'
        "<description>Test feed</description>" + "".join(items) + "</channel></rss>"
    )


def test_fixtures_parse_into_items(mock_http):
    '''Sanity check that each saved real response is read in full.'''
    result = fetch_feeds(ALL_FEEDS, limit=1000)

    assert result.failed_feeds == []
    counts = {name: sum(i.source == name for i in result.items) for name in ALL_FEEDS}
    assert counts == {"bbc_world": 33, "hindu_international": 60, "hindu_national": 60}
    # BBC links carry ?at_medium=RSS&at_campaign=rss tracking params, which must be dropped.
    assert all("?" not in i.link for i in result.items)


# --- keyword filter ---------------------------------------------------------

def test_keyword_filter_is_case_insensitive_and_spans_feeds(mock_http):
    result = fetch_feeds(ALL_FEEDS, keyword="NVIDIA", limit=20)

    assert result.failed_feeds == []
    assert len(result.items) == 12
    assert {i.source for i in result.items} == {"bbc_world", "hindu_international"}
    sources = [i.source for i in result.items]
    assert sources.count("bbc_world") == 4
    assert sources.count("hindu_international") == 8
    for item in result.items:
        assert "nvidia" in item.title.lower() or "nvidia" in item.summary.lower()


def test_keyword_matches_summary_only(mock_http):
    '''A match in the summary counts even when the title doesn't contain the keyword.'''
    mock_http.get(FeedURLs["bbc_world"]).mock(
        return_value=rss_response(
            rss(
                "<item><title>Talks resume</title><link>https://example.com/a</link>"
                "<description>Delegations meet in Abu Dhabi.</description></item>",
                "<item><title>Weather update</title><link>https://example.com/b</link>"
                "<description>Rain expected.</description></item>",
            )
        )
    )

    result = fetch_feeds(["bbc_world"], keyword="abu dhabi")

    assert [i.title for i in result.items] == ["Talks resume"]


def test_keyword_with_no_matches_returns_empty_without_errors(mock_http):
    result = fetch_feeds(ALL_FEEDS, keyword="zzz-no-such-word-zzz")

    assert result.items == []
    assert result.failed_feeds == []


# --- sorting ----------------------------------------------------------------

def test_items_sorted_newest_first_across_feeds_and_timezones(mock_http):
    '''
    BBC dates are GMT, The Hindu's are +0530. Sorting must compare them in UTC:
    the newest item overall is The Hindu's 12:36:10 +0530 (07:06:10 UTC), which
    beats BBC's newest at 03:58:13 GMT.
    '''
    result = fetch_feeds(ALL_FEEDS, limit=1000)

    dates = [i.published_utc for i in result.items]
    assert None not in dates
    assert dates == sorted(dates, reverse=True)

    newest = result.items[0]
    assert newest.source == "hindu_international"
    assert newest.title.startswith("Bill in U.S. Congress to recognise Deepavali")
    assert newest.published_utc == datetime(2026, 11, 10, 7, 6, 10, tzinfo=timezone.utc)

    oldest = result.items[-1]
    assert oldest.source == "bbc_world"
    assert oldest.published_utc == datetime(2026, 11, 8, 0, 48, 59, tzinfo=timezone.utc)


def test_limit_keeps_the_newest_items(mock_http):
    everything = fetch_feeds(ALL_FEEDS, limit=1000).items
    top5 = fetch_feeds(ALL_FEEDS, limit=5).items

    assert [i.link for i in top5] == [i.link for i in everything[:5]]


# --- HTML stripping ---------------------------------------------------------

def test_html_is_stripped_from_title_and_summary(mock_http):
    item = (
        "<item>"
        # "&amp;amp;" is decoded twice on purpose: feedparser turns it into "&amp;", then
        # _clean_text's html.unescape turns that into "&". Feeds double-escape like this in
        # the wild; removing either decode step will (correctly) break this assertion.
        "<title>&lt;b&gt;Breaking:&lt;/b&gt; Summit &amp;amp; talks</title>"
        "<link>https://example.com/story?utm_source=rss</link>"
        "<description><![CDATA["
        '<p>Leaders <a href="https://example.com/x">met</a> in <strong>Geneva</strong>.</p>'
        # \u00a0 (no-break space) must become a plain space via NFKC; \u200b (zero-width space) must be dropped.
        "<p>&lt;em&gt;Escaped&lt;/em&gt;\u00a0markup\u200b and &quot;entities&quot; too.</p>"
        '<img src="https://example.com/pic.jpg"/>'
        "]]></description>"
        "<pubDate>Tue, 10 Nov 2026 03:00:00 GMT</pubDate>"
        "</item>"
    )
    mock_http.get(FeedURLs["bbc_world"]).mock(return_value=rss_response(rss(item)))

    [result] = fetch_feeds(["bbc_world"]).items

    assert result.title == "Breaking: Summit & talks"
    assert result.summary == 'Leaders met in Geneva. Escaped markup and "entities" too.'
    assert result.link == "https://example.com/story"
    assert "<" not in result.summary and ">" not in result.summary


def test_long_summary_is_truncated_at_word_boundary(mock_http):
    words = " ".join(f"word{n}" for n in range(200))
    mock_http.get(FeedURLs["bbc_world"]).mock(
        return_value=rss_response(
            rss(f"<item><title>Long</title><link>https://example.com/l</link><description>{words}</description></item>")
        )
    )

    [result] = fetch_feeds(["bbc_world"]).items

    assert len(result.summary) <= 300
    assert result.summary.endswith("…")
    assert result.summary[:-1] in words and words.startswith(result.summary[:-1] + " ")


# --- missing date -----------------------------------------------------------

def test_missing_date_gives_none_and_sorts_last(mock_http):
    mock_http.get(FeedURLs["bbc_world"]).mock(
        return_value=rss_response(
            rss(
                "<item><title>Undated</title><link>https://example.com/u</link><description>x</description></item>",
                "<item><title>Older</title><link>https://example.com/o</link><description>x</description>"
                "<pubDate>Mon, 09 Nov 2026 10:00:00 GMT</pubDate></item>",
                "<item><title>Garbled date</title><link>https://example.com/g</link><description>x</description>"
                "<pubDate>sometime last week</pubDate></item>",
                "<item><title>Newer</title><link>https://example.com/n</link><description>x</description>"
                "<pubDate>Tue, 10 Nov 2026 10:00:00 GMT</pubDate></item>",
            )
        )
    )

    result = fetch_feeds(["bbc_world"])

    assert result.failed_feeds == []
    assert [i.title for i in result.items[:2]] == ["Newer", "Older"]
    assert {i.title for i in result.items[2:]} == {"Undated", "Garbled date"}
    assert all(i.published_utc is None for i in result.items[2:])


# --- timeout ----------------------------------------------------------------

def test_timeout_is_reported_and_other_feeds_still_return(mock_http):
    mock_http.get(FeedURLs["bbc_world"]).mock(side_effect=httpx.ReadTimeout("timed out"))

    result = fetch_feeds(["bbc_world", "hindu_international"], limit=1000)

    assert result.failed_feeds == [
        FeedError(feed="bbc_world", reason="timeout", detail="The source did not respond in time.")
    ]
    assert len(result.items) == 60
    assert {i.source for i in result.items} == {"hindu_international"}


def test_connect_timeout_is_also_a_timeout(mock_http):
    mock_http.get(FeedURLs["hindu_national"]).mock(side_effect=httpx.ConnectTimeout("connect timed out"))

    result = fetch_feeds(["hindu_national"])

    assert result.items == []
    assert [(f.feed, f.reason) for f in result.failed_feeds] == [("hindu_national", "timeout")]


# --- HTTP status ------------------------------------------------------------

def test_http_404_is_reported_and_other_feeds_still_return(mock_http):
    mock_http.get(FeedURLs["bbc_world"]).mock(return_value=httpx.Response(404, text="Not Found"))

    result = fetch_feeds(["bbc_world", "hindu_international"], limit=1000)

    assert result.failed_feeds == [
        FeedError(feed="bbc_world", reason="http_404", detail="The source returned HTTP 404.")
    ]
    assert len(result.items) == 60
    assert {i.source for i in result.items} == {"hindu_international"}


def test_http_5xx_reason_carries_the_status(mock_http):
    mock_http.get(FeedURLs["hindu_national"]).mock(return_value=httpx.Response(503, text="Service Unavailable"))

    result = fetch_feeds(["hindu_national"])

    assert result.items == []
    assert [(f.feed, f.reason) for f in result.failed_feeds] == [("hindu_national", "http_503")]


def test_error_status_with_a_valid_feed_body_is_still_a_failure(mock_http):
    '''The status check runs before parsing, so a feed body on a 500 is not treated as news.'''
    mock_http.get(FeedURLs["bbc_world"]).mock(
        return_value=httpx.Response(500, content=load_fixture("bbc_world"))
    )

    result = fetch_feeds(["bbc_world"])

    assert result.items == []
    assert [(f.feed, f.reason) for f in result.failed_feeds] == [("bbc_world", "http_500")]


# --- connection error -------------------------------------------------------

def test_connection_error_is_reported_and_other_feeds_still_return(mock_http):
    mock_http.get(FeedURLs["hindu_international"]).mock(
        side_effect=httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    )

    result = fetch_feeds(["hindu_international", "hindu_national"], limit=1000)

    assert result.failed_feeds == [
        FeedError(feed="hindu_international", reason="connection_error", detail="Could not connect to the source.")
    ]
    assert len(result.items) == 60
    assert {i.source for i in result.items} == {"hindu_national"}


# --- unknown error ----------------------------------------------------------

def test_unexpected_exception_is_unknown_error_and_does_not_leak_details(mock_http):
    '''A non-httpx exception is caught per feed; its message stays in the server log only.'''
    mock_http.get(FeedURLs["bbc_world"]).mock(side_effect=RuntimeError("internal detail: /var/task/secret"))

    result = fetch_feeds(["bbc_world", "hindu_national"], limit=1000)

    assert result.failed_feeds == [
        FeedError(
            feed="bbc_world",
            reason="unknown_error",
            detail="An unexpected error occurred while processing the source.",
        )
    ]
    assert "secret" not in result.failed_feeds[0].detail
    assert len(result.items) == 60


# --- malformed XML ----------------------------------------------------------

def test_malformed_xml_is_reported_and_other_feeds_still_return(mock_http):
    '''The real BBC response cut off inside the channel header, before any <item>.'''
    truncated = load_fixture("bbc_world")[:400]
    mock_http.get(FeedURLs["bbc_world"]).mock(return_value=rss_response(truncated))

    result = fetch_feeds(["bbc_world", "hindu_national"], limit=1000)

    assert result.failed_feeds == [
        FeedError(
            feed="bbc_world",
            reason="malformed_feed",
            detail="The source's response could not be read as a news feed.",
        )
    ]
    assert len(result.items) == 60
    assert {i.source for i in result.items} == {"hindu_national"}


def test_html_error_page_with_200_is_malformed(mock_http):
    '''A 200 response that isn't a feed at all (no feedparser version) must not read as "no news".'''
    mock_http.get(FeedURLs["bbc_world"]).mock(
        return_value=httpx.Response(
            200, content=load_fixture("html_error_page", ".html"), headers={"Content-Type": "text/html; charset=utf-8"}
        )
    )

    result = fetch_feeds(["bbc_world"])

    assert result.items == []
    assert [(f.feed, f.reason) for f in result.failed_feeds] == [("bbc_world", "malformed_feed")]


def test_garbage_body_is_malformed(mock_http):
    mock_http.get(FeedURLs["hindu_international"]).mock(
        return_value=rss_response("<rss><channel><title>x</titl></channel></rss>")
    )

    result = fetch_feeds(["hindu_international"])

    assert result.items == []
    assert [(f.feed, f.reason) for f in result.failed_feeds] == [("hindu_international", "malformed_feed")]


# --- incomplete feed --------------------------------------------------------

def test_truncated_feed_keeps_items_and_reports_incomplete(mock_http):
    '''The real BBC response cut off halfway: the 16 items before the break are kept, and flagged.'''
    mock_http.get(FeedURLs["bbc_world"]).mock(return_value=rss_response(load_fixture("truncated_bbc")))

    result = fetch_feeds(["bbc_world", "hindu_national"], limit=1000)

    assert result.failed_feeds == [
        FeedError(
            feed="bbc_world",
            reason="incomplete_feed",
            detail="The source's feed was incomplete; some items may be missing.",
        )
    ]
    sources = [i.source for i in result.items]
    assert sources.count("bbc_world") == 16
    assert sources.count("hindu_national") == 60


def test_incomplete_is_reported_even_when_keyword_filters_out_its_items(mock_http):
    '''"No matches" from a broken feed is not the same as "no matches", so the flag must survive filtering.'''
    mock_http.get(FeedURLs["bbc_world"]).mock(return_value=rss_response(load_fixture("truncated_bbc")))

    result = fetch_feeds(["bbc_world"], keyword="zzz-no-such-word-zzz")

    assert result.items == []
    assert [(f.feed, f.reason) for f in result.failed_feeds] == [("bbc_world", "incomplete_feed")]


# --- valid but empty / harmless bozo ----------------------------------------

def test_valid_feed_with_no_items_is_no_news_not_an_error(mock_http):
    mock_http.get(FeedURLs["bbc_world"]).mock(return_value=rss_response(load_fixture("empty_valid_feed")))

    result = fetch_feeds(["bbc_world"])

    assert result.items == []
    assert result.failed_feeds == []


def test_harmless_encoding_warning_is_not_flagged(mock_http):
    '''
    The real BBC feed declaring encoding="us-ascii" while containing UTF-8 characters:
    feedparser sets bozo with CharacterEncodingOverride but parses everything.
    That must not be reported as incomplete or malformed.
    '''
    body = load_fixture("encoding_override")
    assert b'encoding="us-ascii"' in body
    assert feedparser.parse(body.decode("utf-8")).bozo  # guard: the fixture really does trigger bozo
    mock_http.get(FeedURLs["bbc_world"]).mock(return_value=rss_response(body))

    result = fetch_feeds(["bbc_world"], limit=1000)

    assert result.failed_feeds == []
    assert len(result.items) == 33


# --- keyword terms and relevance ranking ------------------------------------

@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("Russia Ukraine war", ["russia", "ukraine"]),
        ("U.S. Iran", ["us", "iran"]),
        ("US Iran", ["us", "iran"]),
        ("latest news", []),
        (None, []),
        ("Iran's war with US", ["iran", "us"]),
        ("WHO", ["who"]),  # World Health Organization, not the pronoun
    ],
)
def test_keyword_terms(keyword, expected):
    assert _keyword_terms(keyword) == expected


def test_us_matches_dotted_us_and_not_unrelated_titles(mock_http):
    mock_http.get(FeedURLs["bbc_world"]).mock(
        return_value=rss_response(
            rss(
                "<item><title>U.S. military action in the Gulf</title><link>https://example.com/a</link>"
                "<description>Officials comment.</description></item>",
                "<item><title>Russia announces new measures</title><link>https://example.com/b</link>"
                "<description>Moscow responds.</description></item>",
            )
        )
    )

    result = fetch_feeds(["bbc_world"], keyword="US")

    assert [i.title for i in result.items] == ["U.S. military action in the Gulf"]


def test_item_matching_more_terms_ranks_above_newer_single_match(mock_http):
    mock_http.get(FeedURLs["bbc_world"]).mock(
        return_value=rss_response(
            rss(
                "<item><title>Iran holds drills</title><link>https://example.com/new</link>"
                "<description>Navy exercises.</description>"
                "<pubDate>Tue, 10 Nov 2026 12:00:00 GMT</pubDate></item>",
                "<item><title>US and Iran resume talks</title><link>https://example.com/old</link>"
                "<description>Envoys meet.</description>"
                "<pubDate>Mon, 09 Nov 2026 12:00:00 GMT</pubDate></item>",
            )
        )
    )

    result = fetch_feeds(["bbc_world"], keyword="US Iran")

    assert [i.title for i in result.items] == ["US and Iran resume talks", "Iran holds drills"]


def test_default_limit_is_five(mock_http):
    assert len(fetch_feeds(ALL_FEEDS).items) == 5
