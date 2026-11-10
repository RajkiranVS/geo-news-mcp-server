'''Thin MCP wrapper around feeds.fetch_feeds.'''
import json
import logging
import os
import secrets
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from feeds import FeedName, FeedResult, fetch_feeds

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


@mcp.tool
def get_feeds(
    feed: Annotated[list[FeedName], Field(min_length=1)],
    keyword: str | None = None,
    limit: Annotated[int, Field(ge=1, le=20)] = 10,
) -> FeedResult:
    '''
    Fetch the latest headlines from one or more news feeds.

    Available feeds:
        bbc_world: BBC News world headlines.
        hindu_international: The Hindu, international news.
        hindu_national: The Hindu, Indian national news.

    Each feed is fetched independently: if one feed fails, items from the others are
    still returned and the failure is listed in failed_feeds with the source, a reason
    code and a short explanation. Tell the user which sources failed and why.
    Empty items with no failed_feeds means no matching news, not an error.

    A feed with reason "incomplete_feed" appears in failed_feeds AND still contributed
    items: its data broke partway through, so its items are returned but some may be
    missing. Present those results as possibly incomplete (e.g. "BBC results may be
    incomplete"), not as the source's full feed.

    Args:
        feed (list[FeedName]): One or more feed names to fetch headlines from.
        keyword (str | None): Case-insensitive keyword matched against the title and summary. If None, no filtering is applied.
        limit (int): The maximum number of news items to return.
    Returns:
        FeedResult: items (newest first) and failed_feeds (feed, reason e.g. "timeout", "http_404", "malformed_feed", "incomplete_feed", and detail).
    '''
    return fetch_feeds(feed, keyword=keyword, limit=limit)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})

class APIKeyMiddleware(BaseHTTPMiddleware):
    '''
    A BaseHTTP Middlewear class to handle API Calls with Key
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
