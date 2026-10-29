# C4 architecture diagrams: Open DJ

*Written Tue 27 Oct 2026 against commit `9f36f1b426` on `main`. Overview prose:
[`../../architecture.md`](../../architecture.md). Area maps: [`../../moc/README.md`](../../moc/README.md).*

This directory holds the C4 model of Open DJ (the `music-dj-tools` repository): a small set of
diagrams that zoom from "what talks to Open DJ" down to "which file owns this". Every box names
the directory or file it maps to, and every arrow says what moves and over what protocol, so a
diagram can be checked against the tree rather than taken on trust.

## The set, and the order to read it

| File | C4 level | Question it answers |
|:-----|:---------|:--------------------|
| [`c4-1-system-context.md`](c4-1-system-context.md) | 1, system context | Who uses Open DJ, and which outside systems does it touch? |
| [`c4-2-containers.md`](c4-2-containers.md) | 2, containers | Which processes and data stores exist, how each starts, and the protocols between them. Read this one first if you read only one. |
| [`c4-3-components-webui-rig.md`](c4-3-components-webui-rig.md) | 3, components | Inside the SvelteKit performance rig: decks, transport clock, audio graph, stems, the IPC dispatcher, agent orders. |
| [`c4-3-components-audio-engine.md`](c4-3-components-audio-engine.md) | 3, components | Inside `odj-audio`, the Rust engine process. |
| [`c4-3-components-daemon.md`](c4-3-components-daemon.md) | 3, components | Inside the Python engine daemon: chassis, routes, state layer, adapters, jobs. |
| [`flow-deck-load-to-audio.md`](flow-deck-load-to-audio.md) | dynamic | Deck load to audible output, and the library read path, as sequences. |
| [`deployment.md`](deployment.md) | deployment | The packaged macOS app versus the dev loop, and where CI runs the product. |

Level 4 (code) is not drawn: the files named in the level 3 boxes are the code view.

## C4 levels used

- **Person**: a human or agent role that uses the system.
- **Software system**: Open DJ as a whole, or an external system it depends on.
- **Container**: a separately running process or a separately stored data set (a process, a
  SQLite file, a directory of artifacts). Not a Docker container.
- **Component**: a module or file group inside one container.

## Mermaid conventions

- Only `flowchart` and `sequenceDiagram` are used. No C4 DSL, so the diagrams render on GitHub
  and in older Mermaid versions.
- Node labels are quoted, use no HTML, no line-break tags, no angle brackets and no
  parentheses. A label reads `name - technology - path`.
- Shapes carry the C4 type, and each section's legend repeats the mapping:

| Shape | Mermaid syntax | C4 element |
|:------|:---------------|:-----------|
| Stadium | `id(["..."])` | Person |
| Rectangle | `id["..."]` | Container, or Open DJ as a system at level 1 |
| Double-edged box | `id[["..."]]` | External software system |
| Cylinder | `id[("...")]` | Data store container |
| Rounded box | `id("...")` | Component |
| Dashed arrow | `-.->` | Optional, opt-in, or not yet the default path |

- Arrow labels say WHAT moves, then OVER WHAT. Arrows point in the direction of the request or
  the data push; a reply is implied unless the label says otherwise.
- Anything the code did not confirm is marked **unconfirmed** in the text and drawn dashed or
  left out, never drawn as fact.

## How to keep these current

When a container is added, removed or changes how it starts (a new process, a new data store,
a new protocol between two processes), update [`c4-2-containers.md`](c4-2-containers.md) in the
same pull request, then [`deployment.md`](deployment.md) if the packaged payload or the dev loop
changes, and the matching level 3 file if the change lands inside one container. A new external
service goes in [`c4-1-system-context.md`](c4-1-system-context.md). Re-stamp the commit and the
date at the top of each file you touched, and keep the "unconfirmed" markers honest: delete one
only when you have read the code that settles it. The shell row in the containers file must
change at the Electron cutover (plan 21-02).
