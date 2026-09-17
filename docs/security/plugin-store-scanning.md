# Plugin and skill store scanning (future)

Part of [security.md](security.md). Status: **design constraints only**. The store is not
built. Research with sources:
[../research/security-plugin-store-scanning-20260914.md](../research/security-plugin-store-scanning-20260914.md).

## The core finding

**Scanning is triage; the sandbox is the security boundary.** Every store that relies on
scanning alone keeps shipping incidents:

- VS Code: GlassWorm, which hid its payload in invisible Unicode.
- Obsidian: no sandbox.
- ClawHub, Feb 2026: 341 of 2,857 agent skills audited were malicious, 335 of them
  installing a macOS stealer.

The stores that hold up (Figma, Chrome MV3, MetaMask Snaps) limit what plugin code
*can do*. Cisco's own skill scanner says "No findings != no risk".

## What we would accept (v1 of the store)

| Artifact | Accepted? | Pipeline |
|---|---|---|
| Data-only mods: controller maps, themes, FX presets | yes | schema validation + Unicode gate |
| Agent skills (`SKILL.md` + scripts) that act through the typed openDJ API | yes | full pipeline below |
| JS/TS UI plugins, FX DSP | yes, only once the sandbox exists | full pipeline + sandbox |
| Python backends, native code | **no** | Python cannot be sandboxed in-process |

## Prerequisites in the app itself (before any third-party code loads)

These are app fixes, not scanning. Without them, a plugin loaded into the main UI origin
could write the user's libraries and call the updater:

1. **CSP** in `apps/desktop/src-tauri/tauri.conf.json`. None is set today.
2. **Per-client auth on the loopback daemon.** `apps/webui/server/share_gate.py` calls
   the daemon "otherwise authless", with full write access for loopback callers.
   Plugins get scoped per-plugin tokens, never the UI's full access.
3. **Narrow the updater capability.** `capabilities/updater.json` grants
   `updater:default` + `process:allow-restart` to `http://127.0.0.1:*/*`, so any
   loopback page can reach it.
4. Plugins run out of the main origin: a sandboxed cross-origin iframe or a WASM VM
   (QuickJS / Extism) behind a postMessage broker that enforces the manifest. FX DSP
   runs as a WASM block processor inside a first-party worklet with a per-block time
   budget, so a slow plugin cannot starve the decks.

## Submission pipeline

```text
manifest (declared permissions) -> PR to public store repo -> CI static scan
 -> LLM review (advisory) -> human review -> CI-built, store-signed artifact
 -> install-time permission prompt -> runtime sandbox -> rescans + kill-switch
```

| Stage | Tool / rule | Before store opens? |
|---|---|---|
| Manifest | `opendj-plugin.json`: id, version, type, permissions (`library:read`, `deck:control`, `net:<host>`...), sha256 per file. No remote code, no `eval` (Chrome MV3 rule) | must |
| Submission | PR to a public store repo; CI builds the artifact (Raycast model), so a hijacked publisher account cannot upload a binary | must |
| Static scan (fail closed) | `cisco-ai-defense/skill-scanner` (offline, strict policy) for skills; Semgrep + DataDog GuardDog for scripts and JS; osv-scanner on plugin lockfiles; gitleaks; a Unicode gate rejecting tag chars U+E0000-E007F, bidi controls, zero-width and Private Use Area; manifest-vs-code check (network call without `net:` permission fails) | must |
| LLM review | skill-scanner `--use-llm` consensus, or a Claude structured-output reviewer comparing stated purpose to behavior. Advisory: PhantomSkill (Jun 2026) showed evasion of automated reviewers | should |
| Human review | first version of every plugin, and every permission increase | must |
| Sign + pin | store signs the CI-built artifact (minisign, same tooling as the updater); client verifies per-file hashes at load; any change = new version + re-consent (fixes "rug-pull" updates) | must |
| Revocation | signed blocklist checked at startup, auto-disable; daily catalog rescans with updated rules | blocklist must, rescans should |

## Revisit paid tooling when the store opens

At that point we become a distributor of third-party code. Socket Team (about $125/mo
minimum) and Chainguard Libraries start to earn their cost. See
[../research/security-paid-vs-oss-20260914.md](../research/security-paid-vs-oss-20260914.md).
