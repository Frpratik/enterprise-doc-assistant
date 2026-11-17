"""
Actions (Phase 7): what the assistant can DO, not just say.

  domain.py        the leave and ticket rules, the reads, and the drafts (a draft changes nothing)
  tool_token.py    the short-lived signed token that tells a tool server WHO the caller is (the model never chooses that)
  mcp_server.py    the MCP server: read tools and propose tools only. There is no tool that writes.
  mcp_client.py    how the agent talks to that server (a separate process, with a least-privilege environment)

The rule of the whole phase: the model can read and PROPOSE; only a person's approval (Stage 2) makes the system write.
"""
