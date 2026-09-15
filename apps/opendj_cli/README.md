# opendj CLI and shipped MCP (AGENT-11)

The `opendj` console script drives a running Open DJ over the AGENT-03 command
bus. On an installed app, the same entrypoint also hosts the stdio MCP server
agents register against.

## Installed app registration

```bash
claude mcp add opendj -- "/Applications/Open DJ.app/Contents/Resources/payload/bin/opendj" mcp
```

Codex equivalent:

```bash
codex mcp add opendj -- "/Applications/Open DJ.app/Contents/Resources/payload/bin/opendj" mcp
```

No repo checkout is required when Open DJ is running and the bundled binary is
used.

## MCP tools

| Tool | Purpose |
|------|---------|
| `status` | Lock-file origin, health, build-info |
| `app_state` | GET-only proxy to `/api/v1/*` |
| `command` | AGENT-03 bus dispatch with mirror deltas |
| `library` | LIBM-11 library HTTP against the engine origin (`body` = JSON string) |
| `ui_url` | Engine-served SPA URL for browser MCP |

Safety rails are enforced in the server: master mute before play, writeback
blocked, destructive library verbs gated unless `OPENDJ_MCP_ENABLE_DESTRUCTIVE=1`
(tests only; do not enable in installed registration).

## Debug shell vs installed app

- Debug Tauri shell with WebDriver: `apps/desktop/mcp/webview-mcp.ts`
- Installed release app over lock-file HTTP: `opendj mcp` (this path)
