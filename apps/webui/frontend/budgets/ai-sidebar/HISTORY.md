# `ai-sidebar` budget history before charges

Write-once record, moved from main's inline `BUDGETS` entry in `scripts/bundle-budget.mjs`
during the Tue 3 Nov 2026 merge of PR #8310 (ADR-NEW-bundle-budget-charges). PR #7892 added
the assistant sidebar as its own marker surface between set-notes-editor and other-lazy.
The root layout mount stays on library; the lazy sidebar chunks live here.

```text
Mon 2 Nov 2026 (PR #7892, merged onto main with #7691): one clean local vite build measured
34,265 (main's shared code grew under the chunks); measured plus 5 percent, ceiled to KiB
=> 36,864. Payback: move the mount into an existing deferred boot hook.
```
