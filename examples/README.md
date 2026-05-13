# Claude Desktop Configuration Examples

Two ready-to-paste snippets for Claude Desktop's MCP config.

## Files

| File | What it enables |
| --- | --- |
| [`claude_desktop_config.read_only.json`](claude_desktop_config.read_only.json) | All 13 read tools. Safe default — no way to change cluster state. |
| [`claude_desktop_config.with_writes.json`](claude_desktop_config.with_writes.json) | All 13 read tools **plus** the 3 write tools (`scale_deployment`, `restart_deployment`, `delete_pod`), restricted to the `dev` and `staging` namespaces. Each write defaults to `dry_run=True`; the LLM must explicitly pass `dry_run=False` to apply. |

## Placeholder to replace

Both files contain `/Users/yourname/.kube/config` — **replace with your actual kubeconfig path** before saving. On Windows that's typically `C:\\Users\\yourname\\.kube\\config` (note the doubled backslashes — JSON requires them).

## Where Claude Desktop's config lives

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Claude Desktop is officially macOS + Windows only. The MCP server itself runs on any platform Python supports (including Linux) — useful with [Claude Code](https://claude.com/claude-code) or other MCP clients that don't ship a desktop GUI.

## Merging with existing servers

Claude Desktop wants ONE `mcpServers` object containing every server. If you already have other MCP servers configured, **don't replace the whole file** — add `"kubernetes"` as a new key under your existing `mcpServers`:

```json
{
  "mcpServers": {
    "your-other-server": { ... },
    "kubernetes": {
      "command": "k8s-mcp-server",
      "args": ["--kubeconfig", "/Users/yourname/.kube/config"]
    }
  }
}
```

After editing, **fully quit and reopen** Claude Desktop (not just close the window — use Cmd-Q on macOS).

## Security warning — `--enable-writes`

The `with_writes.json` example enables **destructive operations**: scale, restart, and delete. Even with all the safeguards (dry-run by default, namespace allowlist, audit logging), think carefully before pointing this at any cluster you care about.

The minimum-surprise recipe:

1. Always use `--namespaces` to restrict the blast radius. The example uses `dev,staging`.
2. Never enable writes against a production cluster from a personal laptop. Run a separate, scoped instance from an audited environment for any production-write workflow.
3. Read [`docs/SECURITY.md`](../docs/SECURITY.md) for the full defense-in-depth model — the five layers, the audit logger contract, and the secret-handling rules.

If you're unsure whether to enable writes, **don't**. Start with the read-only example and graduate later.
