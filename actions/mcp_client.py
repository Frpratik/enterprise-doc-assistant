"""
How the agent talks to the tool server (Phase 7, Stage 1).

  async with ToolSession(principal) as tools:
      await tools.list_tools()
      await tools.call("get_leave_balance", {})

The server runs as a SEPARATE PROCESS for ONE person, started with a short-lived tool token. Its environment is built from scratch: it gets the token, the
database address and the signing secret it needs to check the token, and nothing else (not the LLM key, not the Redis address). A process is not a
sandbox: the server can still read files the user can read, so treat it as separation of duties, not as containment.
Results are size-capped and tool errors are raised as ToolError, so a tool can never hand the agent more than ~20 KB or an unreadable blob.
"""
import json
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from actions.tool_token import issue_tool_token
from indexing.access import Principal
from ingestion.config import PROJECT_ROOT, settings

MAX_RESULT_CHARS = 20_000


class ToolError(Exception):
    """A tool refused or failed. The message is safe to show to the model (it never contains a stack trace)."""


def build_env(token: str) -> dict[str, str]:
    """The ONLY variables the tool server gets (on top of the few the SDK always passes: PATH and friends)."""
    return {"EDA_TOOL_TOKEN": token, "AUTH_SECRET": settings.auth_secret, "DATABASE_URL": settings.database_url, "PYTHONPATH": str(PROJECT_ROOT),
            "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


class ToolSession:
    def __init__(self, principal: Principal, token: str | None = None, timeout: float = 20.0):
        self.principal, self._token, self.timeout, self._client = principal, token, timeout, None

    async def __aenter__(self) -> "ToolSession":
        token = self._token or issue_tool_token(self.principal)
        params = StdioServerParameters(command=sys.executable, args=["-m", "actions.mcp_server"], env=build_env(token), cwd=str(PROJECT_ROOT))
        self._client = Client(params, read_timeout_seconds=self.timeout)
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.__aexit__(*exc)

    async def list_tools(self) -> list:
        return (await self._client.list_tools()).tools

    async def call(self, name: str, arguments: dict | None = None) -> dict:
        result = await self._client.call_tool(name, arguments or {})
        text = "".join(getattr(c, "text", "") for c in result.content)
        if len(text) > MAX_RESULT_CHARS:
            raise ToolError(f"the result of {name} is too large ({len(text)} characters)")
        if result.is_error:
            raise ToolError(text[:500] or f"{name} failed")
        data = result.structured_content
        if data is None:
            try:
                data = json.loads(text)
            except ValueError as e:
                raise ToolError(f"{name} returned something that is not JSON") from e
        return data


class InProcessToolSession(ToolSession):
    """The SAME tool server code, connected in this process instead of through stdio: for tests that exercise many agent runs without starting a
    process each time. Everything else (the schemas, the checks on every call, the size cap) is identical."""

    def __init__(self, principal: Principal, expires_in: float = 300.0, token_version: int | None = None, timeout: float = 20.0):
        super().__init__(principal, None, timeout)
        self.expires_in, self.token_version = expires_in, token_version

    async def __aenter__(self) -> "InProcessToolSession":
        import time

        from actions.mcp_server import build_server
        self._client = Client(build_server(self.principal, time.time() + self.expires_in, self.token_version))
        await self._client.__aenter__()
        return self
