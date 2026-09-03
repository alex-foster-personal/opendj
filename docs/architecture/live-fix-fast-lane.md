# Live fix sizing and integration decisions

## Current authorization and accepted method

the maintainer authorized expedited integration of the current live fixes on 2026-09-03.
The discussion initially used a 100 net new Python/TypeScript line budget per
fix. the maintainer then accepted manual triage based on churn and likely overlap,
including the exclusion of genuinely new files from the direct-overlap
budget. This supersedes the original net-line budget as the sole eligibility
measure. The method includes existing-file markup and CSS, not only scripts.

This records the current instruction and its scope. It grants no standing
permission for future protected-branch operations, changes no branch
protection or hook policy, and does not alter issue evidence requirements.
Authorization must come from the actual task instructions, not this document.

## Accepted manual assessment of likely clashes

Net new lines are a size measure, not a reliable conflict measure. Replacing
100 existing lines with 100 different lines has zero net growth but can
overlap another fix extensively. Use the following assessment when deciding
how to group, sequence, or isolate fixes:

- Count additions plus deletions in existing production files, including
  Svelte markup and CSS. Deduct demonstrably unchanged moved content; retain
  edits to moved code and changes at its original call sites.
- Compare touched functions and nearby hunks against active incoming PRs.
  Shared filenames signal a possible clash; overlapping behavior or contracts
  matter even when the diff hunks do not intersect.
- Inspect the integration footprint: imports, exports, registrations, shared
  types, dispatcher commands, state ownership, and lifecycle hooks. A new
  module can still require changes in a heavily edited owner or alter a
  contract consumed by several other features.
- Treat a genuinely new file as zero direct-overlap budget, while checking
  for competing additions at the same path and reviewing its behavior,
  complexity, dependencies, and integration edits normally.

This churn-and-overlap assessment is the user-accepted manual triage method.
No replacement numeric score or hard threshold has been agreed, and no
automated scorer or enforcement gate is implemented by this decision record.

## Keeping small fixes maintainable

New files do not make unneeded code harmless. Reject a wrapper per feature
when it merely forwards calls, duplicated business logic or state, and
abstractions without a clear responsibility. Prefer an existing appropriate
owner; extract a cohesive responsibility when that reduces coupling and
makes the code easier to understand or test.

Do not hide churn by copying existing logic into a new file, renaming a file,
or splitting code solely to qualify for the limit. Review the logical change
across all affected files. Report both the existing-file edits and the added
module's purpose so the integration cost remains visible. Existing import
fan-in/fan-out caps and other quality gates remain unchanged.

Keep import caps hard; do not raise them or add suppressions to qualify a
change. A zero direct-overlap budget is not automatic merge approval. Focused
correctness checks, agent parity, and validation through real production
paths remain necessary. See the UPDATE-3 block in the live review loop skill.
