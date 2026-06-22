# @pytheum/mcp

npm wrapper for the pytheum prediction-market MCP server. Use this if your MCP client only knows how to run `npx` (e.g. Claude Desktop on a stock Mac).

## Why a wrapper?

The pytheum MCP server is a Python project — it ships inside the [`pytheum`](https://pypi.org/project/pytheum/) PyPI package as the `pytheum-mcp` console script (entry point `pytheum.mcp.server:main`). This npm package is a tiny zero-dependency Node shim that spawns it via [`uv`](https://docs.astral.sh/uv/) / `uvx` (`uvx --from pytheum pytheum-mcp`).

It exists because Claude Desktop bundles Node.js but not Python (see [mcpb#89](https://github.com/modelcontextprotocol/mcpb/issues/89)), so the easiest way to install an MCP server there is `npx -y <pkg>`. This shim makes that work.

> Prefer no install at all? Use the hosted remote connector — add `https://api.pytheum.com/mcp` as a custom MCP connector. The npm/PyPI path is only for clients that need a local stdio process.

## Install / use

Add this to your `claude_desktop_config.json` (macOS path: `~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "pytheum": {
      "command": "npx",
      "args": ["-y", "@pytheum/mcp"]
    }
  }
}
```

Requires [`uv`](https://docs.astral.sh/uv/) on the system (the shim spawns the Python server through it):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Direct (Python) install

If your client can run Python directly, skip the npm shim:

```bash
pip install pytheum
pytheum-mcp        # stdio MCP server
```
