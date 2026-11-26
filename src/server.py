'''Thin MCP wrapper around feeds.fetch_feeds.'''
import json
import logging
import os
import secrets
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from feeds import FEEDS, FeedName, FeedResult, fetch_feeds

from starlette.requests import Request
from starlette.responses import JSONResponse

from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware


def _parse_secret(secret: str) -> str:
    '''
    The API key from a Secrets Manager SecretString. Accepts either key/value
    JSON ({"api_key": "..."}) or the bare key as plaintext, so the secret can
    move between formats without breaking running Lambdas.
    '''
    if not secret.lstrip().startswith("{"):
        return secret
    try:
        data = json.loads(secret)
    except json.JSONDecodeError:
        # Don't chain or echo the secret's contents into logs.
        raise RuntimeError("Secret MCP_API_KEY looks like JSON but does not parse") from None
    key = data.get("api_key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key:
        raise RuntimeError('Secret MCP_API_KEY is JSON but has no non-empty "api_key" field')
    return key


def _load_api_key() -> str:
    '''
    MCP_API_KEY from the environment if set (local runs, tests), otherwise from
    Secrets Manager. The secret may be plaintext or JSON; see _parse_secret.
    '''
    key = os.environ.get("MCP_API_KEY")
    if key:
        return key
    import botocore.session
    from aws_secretsmanager_caching import SecretCache, SecretCacheConfig

    client = botocore.session.get_session().create_client("secretsmanager", region_name="us-east-1")
    cache = SecretCache(config=SecretCacheConfig(), client=client)
    secret = cache.get_secret_string("MCP_API_KEY")
    if not secret:
        raise RuntimeError("Secret MCP_API_KEY has no SecretString")
    return _parse_secret(secret)


MCP_API_KEY = _load_api_key()

mcp = FastMCP(
    "Geo Political News Feeder",
    instructions="A tool to fetch the latest geopolitical news headlines from listed sources.",
)

_FEED_LIST = "\n".join(f"- {name}: {feed.about}" for name, feed in FEEDS.items())


GET_FEEDS_DESCRIPTION = f"""\
Fetch the latest news headlines from one or more feeds.

Feeds (pass one or more in `feed`):
{_FEED_LIST}

keyword (optional): words to look for in each item's title and summary. Matching is \
case-insensitive and on whole words; generic words such as "news", "war", "latest" or \
"the" are ignored. An item is returned only if it contains EVERY word of the keyword. \
Country names also match their short and adjective forms (e.g. "UK" matches \
"Britain"/"British", "Iran" matches "Iranian"), and some abbreviations are expanded \
("Indo-Pak" needs India and Pakistan, "DPRK" needs North and Korea, "HK" needs Hong \
and Kong). Use only the essential words: each extra word \
narrows the results. Without a keyword, or \
if it contains only generic words, nothing is filtered and items are newest first.

limit: maximum number of items to return across all feeds. Default 5, maximum 10.

Returns:
- items: each with source, title, link, published_utc and summary (cut to about 300 \
characters). published_utc is the date the source gives for the item; some sources \
put the last update time there rather than first publication, so don't present it as \
the original publication time. It is null if the source gives no date.
- failed_feeds: one entry per feed that had a problem, with feed, reason and a short \
detail. Each feed is fetched independently, so a failed feed does not stop the others: \
items from the feeds that worked are still returned. Reasons: "timeout", \
"http_<status>" (e.g. "http_404"), "connection_error", "malformed_feed", \
"incomplete_feed", "unknown_error". Tell the user which sources failed and why.

"incomplete_feed" is the one failure that still returns items: the source's feed broke \
partway through, so that source's items are included but some of its results may be \
missing. Present them as possibly incomplete (e.g. "BBC results may be incomplete").

An empty items list with no failed_feeds means there is no matching news right now. \
It is not an error; say so rather than retrying or reporting a failure.
"""
FEED_PARAM_DESCRIPTION = "One or more feeds to fetch. " + "; ".join(
    f"{name}: {feed.about}" for name, feed in FEEDS.items()
)

@mcp.tool(description=GET_FEEDS_DESCRIPTION)
def get_feeds(
    feed: Annotated[
        list[FeedName], # type: ignore[valid-type]
        Field(
            min_length=1,
            max_length=len(FEEDS),
            description=FEED_PARAM_DESCRIPTION,
        ),
    ],
    keyword: Annotated[
        str | None,
        Field(
            description="Optional words to match in item titles and summaries (case-insensitive, "
            "whole words; an item must match EVERY word, and country adjectives like 'Iranian' count; generic words like 'news' or 'war' are ignored). "
            "Omit to return all items, newest first.",
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, le=10, description="Maximum number of items to return across all feeds (1-10)."),
    ] = 5,
) -> FeedResult:
    '''Tool wrapper around fetch_feeds. The agent-facing docs are GET_FEEDS_DESCRIPTION.'''
    return fetch_feeds(feed, keyword=keyword, limit=limit)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})

class APIKeyMiddleware(BaseHTTPMiddleware):
    '''
    A BaseHTTP Middleware class to handle API Calls with Key
    '''
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        api_key = request.headers.get("x-api-key")
        if (
            not isinstance(MCP_API_KEY, str)
            or not isinstance(api_key, str)
            or not secrets.compare_digest(MCP_API_KEY.encode(), api_key.encode())
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

# Create the ASGI app last: http_app() only includes routes registered before it runs.
app = mcp.http_app(stateless_http=True, json_response=True, middleware=[Middleware(APIKeyMiddleware)])
