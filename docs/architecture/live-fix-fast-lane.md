# Live fix sizing and integration decisions

## Current authorization and counting rule

the maintainer authorized expedited integration of the current live fixes on 2026-09-03.
The discussion used a 100 net new Python/TypeScript line budget per fix,
excluding tests, documentation, and genuine moves or file splits. His later
clarification also excludes new files from that change limit because they
usually have little direct textual overlap with incoming work. Count Svelte
script blocks as TypeScript; report markup and CSS separately under this rule.

This records the current instruction and its scope. It grants no standing
permission for future protected-branch operations, changes no branch
protection or hook policy, and does not alter issue evidence requirements.
Authorization must come from the actual task instructions, not this document.

## Recommended assessment of likely clashes

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

This churn-and-overlap assessment is a recommendation. No numeric score,
replacement threshold, automated classifier, or enforcement gate is
implemented by this decision record.

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
