# `set-notes-editor` budget history before charges

Write-once record, moved verbatim from the comment above this surface's `BUDGETS` entry in
`scripts/bundle-budget.mjs` on Tue 3 Nov 2026 (ADR-NEW-bundle-budget-charges). The last line
is the entry itself. Its limit, 81920, is this surface's base.json limit unchanged; its
recorded `measured` was 77306. New growth is a charge file, never an edit here.

```text
The Set notes editor (PR #6340): ProseMirror, imported only on the pad's first open, so no first-paint
cost. Stays between performance and other-lazy for the same reason as any lazy feature bucket: a file is
charged to the first budget that reaches it. Mon 2 Nov 2026, one clean vite build on main 638ea5a1da.
Limit: measured plus 5 percent, ceiled to KiB. Payback: none planned; the editor is the feature.
{ name: 'set-notes-editor', limit: 81920, measured: 77306, note: 'Set notes editor, lazy on first open' }
```
