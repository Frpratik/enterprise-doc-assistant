"""
The MCP tool server (Phase 7, Stage 1): what the model is allowed to call.

  READ     get_leave_balance, list_my_leave_requests, list_my_tickets, get_ticket          the caller's OWN data only
  PROPOSE  propose_leave_request, propose_ticket                                          check everything, change NOTHING, return a draft

There is deliberately NO tool that writes. A draft is turned into a real leave request or ticket only by a person's approval (Stage 2), through a
path the model cannot call. Every tool is annotated read-only, takes no user or company argument (the identity comes from the tool token, see
tool_token.py) and is checked again on every call: the person must still be active and the token not expired.

Run as a process:  EDA_TOOL_TOKEN=<token> python -m actions.mcp_server   (speaks MCP over stdin/stdout; everything else goes to stderr)
"""
import os
import sys
import time
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from actions import domain
from actions.tool_token import ToolAuthError, verify_tool_token
from indexing.access import Principal
from indexing.users import get_user

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
TOOL_NAMES = ("get_leave_balance", "list_my_leave_requests", "list_my_tickets", "get_ticket", "propose_leave_request", "propose_ticket")

LeaveType = Literal["sick", "casual", "earned"]
Category = Literal["hardware", "software", "access", "network", "other"]
Priority = Literal["low", "normal", "high"]
LeaveStatus = Literal["pending_manager", "approved", "rejected", "cancelled"]
TicketStatus = Literal["open", "in_progress", "resolved", "closed"]
IsoDate = Annotated[str, Field(min_length=10, max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$", description="a date like 2026-12-07")]


def build_server(principal: Principal, expires_at: float, token_version: int | None = None) -> MCPServer:
    """A tool server for ONE person. The person is fixed here; no tool can change it."""
    server = MCPServer("enterprise-actions", instructions="Tools for the signed-in employee's own leave and IT tickets. Tools only read or propose; a person approves any change.")

    def me() -> Principal:
        """Checked on EVERY call: the session must not have expired and the person must still be active in the same company."""
        if time.time() >= expires_at:
            raise ToolAuthError("the tool session expired")
        user = get_user(principal.user_id)
        if user is None or not user.is_active or user.tenant != principal.tenant or (token_version is not None and user.token_version != token_version):
            raise ToolAuthError("the person is no longer active or their sign-in state changed")
        return Principal(user.tenant, user.roles, user_id=user.user_id)

    @server.tool(annotations=READ_ONLY)
    def get_leave_balance() -> dict:
        """Your leave balance this year: for each leave type the annual days, days taken, days waiting for approval and days remaining."""
        return {"balances": domain.balances(me().user_id)}

    @server.tool(annotations=READ_ONLY)
    def list_my_leave_requests(status: LeaveStatus | None = None) -> dict:
        """Your own leave requests, newest first (at most 20), optionally only those with one status."""
        return {"requests": domain.my_leave_requests(me().user_id, status)}

    @server.tool(annotations=READ_ONLY)
    def list_my_tickets(status: TicketStatus | None = None) -> dict:
        """Your own IT tickets, newest first (at most 20), optionally only those with one status."""
        return {"tickets": domain.my_tickets(me().user_id, status)}

    @server.tool(annotations=READ_ONLY)
    def get_ticket(ticket_id: Annotated[str, Field(min_length=3, max_length=20, pattern=r"^TCK-\d+$")]) -> dict:
        """One of your own tickets by its number, like TCK-1001. A ticket that is not yours is reported as not found."""
        ticket = domain.get_my_ticket(me().user_id, ticket_id)
        return {"found": ticket is not None, "ticket": ticket}

    @server.tool(annotations=READ_ONLY)
    def propose_leave_request(leave_type: LeaveType, start_date: IsoDate, end_date: IsoDate,
                              reason: Annotated[str, Field(min_length=3, max_length=500)]) -> dict:
        """Check a leave request and return a DRAFT of what would be created, or the reasons it cannot be. Nothing is created or sent: a person must approve the draft."""
        return domain.propose_leave(me(), leave_type, start_date, end_date, reason).to_dict()

    @server.tool(annotations=READ_ONLY)
    def propose_ticket(category: Category, title: Annotated[str, Field(min_length=3, max_length=120)],
                       description: Annotated[str, Field(min_length=3, max_length=2000)], priority: Priority = "normal") -> dict:
        """Check an IT ticket and return a DRAFT of what would be created, or the reasons it cannot be. Nothing is created or sent: a person must approve the draft."""
        return domain.propose_ticket(me(), category, title, description, priority).to_dict()

    return server


def main() -> int:
    token = os.environ.get("EDA_TOOL_TOKEN", "")
    try:
        principal, claims = verify_tool_token(token)
    except ToolAuthError as e:
        print(f"tool server not started: {e}", file=sys.stderr)
        return 2
    build_server(principal, float(claims["exp"]), claims["tv"]).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
