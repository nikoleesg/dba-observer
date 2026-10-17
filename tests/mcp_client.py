"""Minimal MCP streamable-HTTP client for the tests and for probing a deployment.

    python tests/mcp_client.py http://127.0.0.1:15000/mcp list
    python tests/mcp_client.py http://127.0.0.1:15000/mcp call mysql_health '{}'
"""

from __future__ import annotations

import itertools
import json
import sys

import httpx


class MCP:
    def __init__(self, url: str, timeout: float = 30):
        self.url = url
        self.http = httpx.Client(timeout=timeout)
        self.ids = itertools.count(1)
        self.headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        init = self._rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "dba-observer-tests", "version": "0"},
        })
        self.server_info = init.get("serverInfo", {})
        self._notify("notifications/initialized")

    def _post(self, body: dict) -> httpx.Response:
        r = self.http.post(self.url, json=body, headers=self.headers)
        if sid := r.headers.get("Mcp-Session-Id"):
            self.headers["Mcp-Session-Id"] = sid
        if pv := r.headers.get("Mcp-Protocol-Version"):
            self.headers["Mcp-Protocol-Version"] = pv
        return r

    def _notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        r = self._post({"jsonrpc": "2.0", "id": next(self.ids), "method": method, "params": params or {}})
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            msg = None
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:])
            assert msg is not None, "empty event stream"
        else:
            msg = r.json()
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg["result"]

    def list_tools(self) -> list[dict]:
        return self._rpc("tools/list")["tools"]

    def call(self, name: str, args: dict | None = None) -> tuple[bool, object]:
        """Returns (is_error, parsed payload). Toolbox returns one text block per row/object."""
        res = self._rpc("tools/call", {"name": name, "arguments": args or {}})
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        parsed = []
        for t in texts:
            try:
                parsed.append(json.loads(t))
            except json.JSONDecodeError:
                parsed.append(t)
        payload = parsed[0] if len(parsed) == 1 else parsed
        return bool(res.get("isError")), payload

    def raw_call(self, name: str, args: dict | None = None) -> str:
        res = self._rpc("tools/call", {"name": name, "arguments": args or {}})
        return json.dumps(res)


if __name__ == "__main__":
    url, cmd = sys.argv[1], sys.argv[2]
    m = MCP(url)
    if cmd == "list":
        for t in m.list_tools():
            print(t["name"])
    elif cmd == "call":
        args = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
        err, payload = m.call(sys.argv[3], args)
        print(json.dumps({"isError": err, "result": payload}, indent=2, default=str))
