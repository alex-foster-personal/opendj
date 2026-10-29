---
term: IPC dispatcher
slug: ipc-dispatcher
aliases: [command dispatcher, performance IPC, dispatch path, command bus]
group: Engineering and product
level: 2
one_liner: One typed doorway every control walks through, so a button press and an agent command do exactly the same thing and both can be checked.
why_it_matters_here: It is what makes Open DJ agent-native; a control that skips it works for your hand but is invisible to automation, tests and recordings.
related: [parity-capture, offline-render, sample-accurate-scheduling, try-er]
sources:
  - Gamma, Helm, Johnson, Vlissides, Design Patterns, Addison-Wesley, 1994 (the Command pattern)
  - Wikipedia, Inter-process communication, https://en.wikipedia.org/wiki/Inter-process_communication
status: draft
---

## In plain words

Every knob, fader and pad in the app sends a small typed message, such as "deck 2, set low EQ to
-12 dB", to one dispatcher. The dispatcher checks the message, queues it behind anything it must
wait for, runs it against the audio engine, and records the result. An agent or a test script
sends the very same message through the same door. Because there is one path, the state the
screen shows and the state an agent reads back can never disagree.

The precise term is a validated, serialized command dispatcher with a matching query surface.
"IPC" (inter-process communication) is loose here: most callers live in the same page.

## Picture

```text
  mouse  MIDI  keyboard         agent  CLI  test
     |     |      |               |     |    |
     +-----+------+               +--+--+----+
           |                         |
           v                         v
     +-------------------------------------+
     |  dispatch  validate > queue > run   | ---> audio graph
     |  query     one snapshot of state    | <--- engine state
     |  capture   record what happened     |
     +-------------------------------------+

  bypass   fader ---------> gain node directly
           works by hand, agent cannot move it or see it,
           query reports a stale value, capture misses it
```

## In Open DJ

The dispatcher is `apps/webui/frontend/src/lib/rb/performance-ipc.svelte.ts`. The page exposes it
as `window.musicDjToolsPerformance` with `dispatch`, `query` and `capture`. Malformed messages
are rejected, each deck has its own queue so one slow load does not block the other, and sync
commands share one coordination scope. `docs/architecture.md` calls a control wired straight to
the engine a defect even when it works. Known debt: some Mixer and TopBar controls do not route
through it yet, tracked in `PARITY-TODO.md`.
