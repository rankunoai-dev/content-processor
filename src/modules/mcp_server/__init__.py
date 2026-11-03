"""Read-only MCP server: Claude Desktop reads the app's projects through the running web app.

`catalog` lists the tools, `bridge` runs them inside the web app (one writer of the app
data, one evidence ledger per project), `server` is the stdio process Claude Desktop starts;
it forwards every call to the web app over local HTTP.
"""
