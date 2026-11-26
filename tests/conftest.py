'''
Shared fixtures. Every test runs inside a respx mock of httpx, so any request
to a URL without a mocked route fails the test instead of reaching the network.
'''
from pathlib import Path

import httpx
import pytest
import respx

from feeds import FEEDS

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str, suffix: str = ".xml") -> bytes:
    '''Raw bytes of tests/fixtures/<name><suffix>.'''
    return (FIXTURES / f"{name}{suffix}").read_bytes()


def rss_response(body: bytes | str) -> httpx.Response:
    '''A 200 response carrying an RSS body, as the real feeds send it.'''
    return httpx.Response(200, content=body, headers={"Content-Type": "application/rss+xml; charset=utf-8"})


@pytest.fixture
def mock_http():
    '''
    respx router with every feed URL mocked to return its saved fixture.
    assert_all_mocked=True (the default) makes unmocked requests raise, so the
    suite cannot touch the network. Tests can override a route via mock_http.get(url).
    '''
    with respx.mock(assert_all_called=False) as router:
        for feed_name, feed in FEEDS.items():
            router.get(feed.url).mock(return_value=rss_response(load_fixture(feed_name)))
        yield router
