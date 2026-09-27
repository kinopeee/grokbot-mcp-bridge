"""Optional MCP server, tools decorator, and ASGI sub-apps."""

from __future__ import annotations

from app import config

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings

    HAS_MCP = True
except ImportError:
    MCPServer = None
    TransportSecuritySettings = None
    HAS_MCP = False

if HAS_MCP:
    mcp = MCPServer(
        "grokbot-bridge",
        instructions=(
            "Bridge to Grok Bot through the Cursor automation webhook. Use ask_grokbot "
            "to ask Grok Bot a question. ask_grokbot waits up to wait_seconds "
            f"(default {config.DEFAULT_ASK_WAIT_SECONDS}, max {config.MAX_WAIT_SECONDS}) "
            "for the callback and returns answer_text with answer_status "
            "pending/answered/cancelled/expired. Pass wait_seconds=0 to return "
            "immediately. If pending, call wait_for_grokbot_answer with the run_id "
            f"(timeout_seconds default {config.DEFAULT_WAIT_TIMEOUT_SECONDS}, "
            f"max {config.MAX_WAIT_SECONDS}; hard cap "
            f"{config.ABSOLUTE_MAX_WAIT_SECONDS}). Use cancel_run to cancel a "
            "pending run so waiters unblock with answer_status=cancelled. "
            "get_grokbot_run and list_grokbot_runs echo the same status/answer_status. "
            "Grok Bot must echo run_id (or request_id) in the callback body. Use "
            "list_grokbot_events/get_grokbot_event to read inbound Grok Bot events "
            "received through the Cursor automation webhook. Write tools "
            "(ask_grokbot, cancel_run) are rate-limited per API key."
        ),
    )
else:
    mcp = None


if mcp is not None:
    tool = mcp.tool
    resource = mcp.resource
else:
    def tool(*_a, **_k):
        def deco(fn):
            return fn
        return deco

    def resource(*_a, **_k):
        def deco(fn):
            return fn
        return deco


def build_mcp_apps():
    if mcp is None or TransportSecuritySettings is None:
        return None, None, None
    if config.ALLOWED_HOSTS:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=config.ALLOWED_HOSTS
            + ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"],
            allowed_origins=[f"https://{h}" for h in config.ALLOWED_HOSTS],
        )
    else:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    streamable_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=transport_security,
    )
    sse_app = mcp.sse_app(
        sse_path="/sse",
        message_path="/messages/",
        transport_security=transport_security,
    )
    return streamable_app, sse_app, streamable_app.router.lifespan_context
