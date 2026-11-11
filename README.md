# Geopolitical News MCP Server

An [MCP](https://modelcontextprotocol.io) server that gives AI agents the latest geopolitical headlines from a fixed allowlist of RSS feeds. It's built with [FastMCP](https://gofastmcp.com) and deploys to AWS Lambda (Function URL plus the [Lambda Web Adapter](https://github.com/aws/aws-lambda-web-adapter)) with AWS SAM to be used by AWS Bedrock AgentCore agent.

## Features

- **Allowlisted sources only.** Agents choose feeds by name, never by URL, so the server can't be used to fetch arbitrary pages.
- **Per-feed error handling.** If one feed fails, the server still returns items from the others and reports the failure with a reason code.
- **Partial-feed recovery.** If a feed's XML breaks partway through, the server keeps the items it read before the break and marks the feed `incomplete_feed`.
- **Clean, agent-friendly output.** HTML and entities are stripped, text is NFKC-normalised, zero-width characters are removed, tracking query strings are dropped from links, and summaries are truncated to 300 characters at a word boundary.
- **Keyword filtering** on title and summary (case-insensitive). Filtering runs before truncation, so it searches the full text.
- **API-key auth** through the `x-api-key` header, compared in constant time. The key comes from AWS Secrets Manager.
- **Stateless HTTP / JSON responses**, which suit Lambda.

## Available feeds

| Name                  | Source                                  |
| --------------------- | --------------------------------------- |
| `bbc_world`           | BBC News: World                         |
| `hindu_international` | The Hindu: International                |
| `hindu_national`      | The Hindu: National (India)             |

Feeds are defined in `FeedURLs` in [src/feeds.py](src/feeds.py). To add a source, add it to both the `FeedName` literal and `FeedURLs`. Also see the note on `_strip_query`: it removes the whole query string, which breaks links for any feed whose article IDs live in the query.

## Tool: `get_feeds`

Fetches the latest headlines from one or more feeds.

| Parameter | Type             | Default | Description                                               |
| --------- | ---------------- | ------- | --------------------------------------------------------- |
| `feed`    | `list[FeedName]` | n/a     | One or more feed names (at least one is required).        |
| `keyword` | `str \| null`    | `null`  | Words matched against title and summary (case-insensitive, whole words, generic words like "news"/"war"/"the" ignored). Items matching more words rank first, then newest. |
| `limit`   | `int` (1–10)     | `5`     | Maximum number of items to return across all feeds.       |

### Response

```json
{
  "items": [
    {
      "source": "bbc_world",
      "title": "Leaders meet in Geneva for ceasefire talks",
      "link": "https://www.bbc.co.uk/news/world-12345678",
      "published_utc": "2026-11-10T08:30:00Z",
      "summary": "Delegations from both sides arrived in Geneva…"
    }
  ],
  "failed_feeds": [
    {
      "feed": "hindu_national",
      "reason": "timeout",
      "detail": "The source did not respond in time."
    }
  ]
}
```

Items are sorted by the number of keyword words they match (most first), then newest first. Items with no publish date are placed last within each group.

`published_utc` is the timestamp the feed reports, converted to UTC. Some sources (BBC and The Hindu among them) move it forward when they edit a story, so it can be the time of the last update rather than the original publish time.

### Failure reasons

| Reason             | Meaning                                                                                  |
| ------------------ | ---------------------------------------------------------------------------------------- |
| `timeout`          | The source didn't respond within 10 seconds.                                             |
| `http_<status>`    | The source returned a non-2xx status, e.g. `http_404`.                                   |
| `connection_error` | The server couldn't connect to the source.                                               |
| `malformed_feed`   | The response wasn't valid RSS/Atom, e.g. an HTML error page served with a 200 status.    |
| `incomplete_feed`  | The XML broke partway through. Items read before the break are still returned in `items`. |
| `unknown_error`    | Any other unexpected error. The full details are logged on the server only.             |

An empty `items` list with an empty `failed_feeds` list means no news matched. It is not an error.

## Project structure

```
.
├── src/
│   ├── feeds.py          # Feed fetching, parsing, cleaning (no MCP dependency)
│   ├── server.py         # FastMCP server, get_feeds tool, /health, API-key middleware
│   ├── run.sh            # Lambda entry point: starts uvicorn on port 8080
│   └── requirements.txt
├── tests/
│   ├── fixtures/         # Captured feed responses plus hand-made failure cases
│   ├── conftest.py       # respx mock shared by every test
│   ├── test_feeds.py
│   └── test_server.py
├── pyproject.toml        # Dependencies and pytest config
├── template.yaml         # AWS SAM template
└── LICENSE
```

## Running locally

Requires Python 3.12. Create the virtual environment at the project root, **not** inside `src/`. `sam build` packages everything under `src/` (`CodeUri: src/`), so a venv there would end up in the Lambda zip.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r src/requirements.txt

export MCP_API_KEY="dev-secret"  # Windows PowerShell: $env:MCP_API_KEY = "dev-secret"
python -m uvicorn server:app --app-dir src --host 0.0.0.0 --port 8080
```

When `MCP_API_KEY` is set, the server uses it and doesn't call Secrets Manager.

Dependencies are managed with [uv](https://docs.astral.sh/uv/), and `uv.lock` is the source of truth. `src/requirements.txt` is generated from `uv.lock` with `uv export --no-hashes --no-dev --no-emit-project -o src/requirements.txt`; don't edit it by hand. After any `uv add` or `uv lock --upgrade`, re-run the export and commit `pyproject.toml`, `uv.lock` and `src/requirements.txt` together.

- MCP endpoint: `http://localhost:8080/mcp`
- Health check: `http://localhost:8080/health` (no auth required)

### Testing with MCP Inspector

```bash
npx @modelcontextprotocol/inspector
```

Choose the **Streamable HTTP** transport, enter `http://localhost:8080/mcp`, and add the header `x-api-key: dev-secret`.

### Testing with curl

```bash
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "x-api-key: dev-secret" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_feeds","arguments":{"feed":["bbc_world"],"keyword":"ukraine","limit":5}}}'
```

## Running tests

With the venv from [Running locally](#running-locally) active, run these from the project root:

```bash
pip install pytest respx
pytest
```

The suite runs entirely offline. Every HTTP call goes through a [respx](https://lundberg.github.io/respx/) mock that serves saved feed responses from `tests/fixtures/`, and unmocked requests fail. It covers every failure reason in the table above (timeouts, `http_<status>` errors, connection errors, malformed XML, HTML error pages served with a 200, and unexpected exceptions, whose details must not leak to the agent), partial recovery from truncated XML, harmless encoding-override warnings, keyword filtering, ordering, summary truncation, and API-key auth. If you recapture the fixtures, update the expected counts as described in [tests/fixtures/README.md](tests/fixtures/README.md).

## Deploying to AWS

### Prerequisites

- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- [Docker](https://docs.docker.com/get-docker/), which `sam build --use-container` uses to build Linux-compatible dependencies
- AWS credentials configured for the target account

### 1. Create the API key secret

Store the secret as **plaintext**, not key/value JSON, under the name `MCP_API_KEY` in **`us-east-1`**:

```bash
aws secretsmanager create-secret \
  --name MCP_API_KEY \
  --secret-string "$(openssl rand -hex 32)" \
  --region us-east-1
```

Note the full ARN in the output. It includes a random suffix.

> The server looks the secret up by the name `MCP_API_KEY` in `us-east-1` (see `_load_api_key` in [src/server.py](src/server.py)). The ARN you pass to the template is used only to grant IAM read access. Both must refer to the same secret, so deploy the stack in `us-east-1` or change the region in `server.py`.

### 2. Build and deploy

```bash
sam build --use-container
sam deploy --guided \
  --parameter-overrides McpApiKeySecretArn=arn:aws:secretsmanager:us-east-1:<account-id>:secret:MCP_API_KEY-XXXXXX
```

`--use-container` is required on Windows and macOS. A plain `sam build` installs compiled packages such as `pydantic-core` for your own OS rather than for Lambda's Linux, and the function then fails at import time.

`--guided` saves your answers to `samconfig.toml`, which is git-ignored.

### 3. Connect

The `McpEndpoint` stack output is the full MCP URL (`https://<id>.lambda-url.<region>.on.aws/mcp`). Point your MCP client at it and send the secret value in the `x-api-key` header.

### What gets deployed

- **Lambda function** (Python 3.12, x86_64, 512 MB, 40 s timeout) running uvicorn behind the Lambda Web Adapter layer. The adapter checks `/health` to know when the app is ready.
- **Function URL** with `AuthType: NONE`. Authentication is enforced by the app's `x-api-key` middleware.
- **IAM permissions** to read and describe the API key secret.
- **CloudWatch log group** with 14-day retention.

## Using it from Bedrock AgentCore

1. In your AgentCore harness, add the `McpEndpoint` URL as a **Remote MCP server** (Streamable HTTP).
2. Configure the connection to send the `x-api-key` header with the secret value on every request.
3. In the agent's system prompt, tell it to call `get_feeds` for current headlines, using the feed names from [Available feeds](#available-feeds) and a `keyword` to narrow the results.

<!-- TODO: link to the agent repo once Phase 3 is done. -->

## Security notes

- **The Function URL is public.** `AuthType: NONE` means anyone with the URL can reach it, and the `x-api-key` check is the only protection. Keep the key out of source control and client-side code.
- **Rotate the key if it leaks.** Update the secret (`aws secretsmanager put-secret-value --secret-id MCP_API_KEY --secret-string ...`), then update the `x-api-key` header in every client. The server reads the key at cold start, so warm instances keep accepting the old key until they recycle. Redeploy or update the function configuration to force new instances.
- **No reserved concurrency.** It isn't set because this account's Lambda concurrency quota is 10, and reserving any of it isn't allowed at that level. Set up an [AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html) alert so you find out about abuse or runaway traffic from the bill.

## Configuration

| Setting             | Location                 | Value                              |
| ------------------- | ------------------------ | ---------------------------------- |
| `MCP_API_KEY`       | env var / Secrets Manager | API key clients must send          |
| `REQUEST_TIMEOUT`   | `src/feeds.py`           | 10 s per feed                      |
| `SUMMARY_MAX_CHARS` | `src/feeds.py`           | 300                                |
| `USER_AGENT`        | `src/feeds.py`           | `geopolitical-briefing-agent/0.1`  |

## License

[MIT](LICENSE) © 2026 Rajkiran Veldur
