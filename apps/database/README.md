# apps/database

The schema document generator for the Open DJ state database.

**Looking for the schema itself? It is not in this directory.** It is rendered
as `AGENTS.md` next to the database file, which for a default checkout is
`data/state/AGENTS.md`:

```bash
python -m apps.database.agents_md              # write it beside the db
python -m apps.database.agents_md --check      # non-zero if stale
```

## Why it lives beside the database, not here

The CLOUDSYNC brief asked for a document that "travels with" the db. A copy
committed here would describe whatever schema the repo is at, and the moment
one machine is a migration behind, that copy is confidently wrong about the
file an agent has actually been handed. Generating it beside the database
makes it correct by construction, and `--check` turns drift into a failure
instead of a surprise.

`AGENTS.md` is the filename on purpose: it is the emerging cross-vendor
convention for agent-readable context, so an agent that has never seen this
project looks for it by name.

## What is hand-written and what is not

| file | maintained by |
|---|---|
| `agents_md.py` | hand: rendering, plus the verbatim brief |
| `descriptions.py` | hand: one sentence per table, saying what a ROW means |
| the rendered `AGENTS.md` | generated: never hand-edit it |

Columns, types, foreign keys, indexes and row counts are introspected from the
live file, so they cannot go stale. Only the per-table sentences are written by
a person, and a table without one renders as `UNDOCUMENTED` and fails
`tests/database/test_agents_md.py`. Adding a table to the schema therefore
forces a decision about how to describe it, which is the entire point.

Where the database lives on each machine is decision D1 in
`specs/cloudsync-spec.md`: the OS application-support directory, overridable
with `MDT_DATA_DIR`.
