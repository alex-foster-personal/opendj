---
term: Desktop shell
slug: desktop-shell
aliases: [app shell, native wrapper, Tauri shell, Electron shell]
group: Engineering and product
level: 2
one_liner: The thin native app that gives a web interface its own window, icon and file picker, while the real work happens in a separate engine.
why_it_matters_here: Open DJ keeps the shell thin so the app behaves the same in its own window or a browser tab, and so the shell can change without touching the mixer.
related: [audioworklet, ipc-dispatcher, offline-render]
sources:
  - Tauri 2 documentation, https://v2.tauri.app/
  - Electron documentation, Process Model, https://www.electronjs.org/docs/latest/tutorial/process-model
status: draft
---

## In plain words

Open DJ's screens are a web page. A desktop shell wraps that page in a normal Mac or Windows app:
a window, a dock icon, a quit menu, an updater, a native folder picker. Tauri does this with the
system's own web view and a small Rust program; Electron does it by bundling its own copy of
Chromium. Either way the shell should stay a window, not an application. The engine process
owns the library, the analysis, the state and the audio decisions.

The split matters for a DJ because a crash or update in one half should not take the other half
down with it, and because anything the shell owns is something a browser tab or an agent cannot
reach.

## Picture

```text
  +-------------- desktop shell ---------------+
  |  window  icon  quit  updater  folder picker |
  |  starts and supervises the engine           |
  |  +---------------------------------------+  |
  |  |  web UI  decks  mixer  browser        |  |
  |  |  same page as a plain browser tab     |  |
  |  +-------------------|-------------------+  |
  +----------------------|----------------------+
                         | HTTP on loopback only
                         v
  +-------------- engine process --------------+
  |  library  state db  analysis  stems        |
  |  every product decision lives here         |
  +--------------------------------------------+
```

## In Open DJ

The Tauri 2 shell is `apps/desktop/src-tauri/`; its README states the thin-shell rule: no
`@tauri-apps/api` in the web app, no Tauri commands holding business logic, Rust owns the window
and nothing else. An Electron shell is being built beside it in `apps/desktop/electron/`
(`(private path)`). It starts or adopts the bundled engine,
opens one window on the loopback origin, and offers four native features through
`window.opendjShell`. The page picks whichever shell is present in
`apps/webui/frontend/src/lib/shell/native-shell.ts`.
