# React twins

Open DJ's v2 UI moves from Svelte to React one component at a time. Each Svelte
component gets a React "twin" that renders the same DOM and behaves the same, proven
by the same Storybook contract. Plan and wave list:
https://claude.ai/artifact/EuqJD1rYELTpnCy8EReCuc (inventory:
`component-inventory.csv` in the project files).

## Files per component

For `src/lib/components/<area>/X.svelte`, the twin adds, in the same folder:

| File | What |
| --- | --- |
| `X.contract.ts` | `export const contracts = { Story: { args, play } }`. Framework-neutral: `storybook/test` only, DOM queries by role, class and text. |
| `X.stories.ts` | The Svelte story. `title: 'Contracts/<Area>/X'` (a literal), each export `= contracts.Story`. |
| `X.tsx` | The React twin. Default export, same props as the Svelte `$props()` (callbacks keep their Svelte names, such as `onrate`). |
| `X.react.css` | The Svelte `<style>` block, every rule anchored on the component's root class so it cannot leak. Omit when the component has no `<style>`. |
| `X.stories.tsx` | The React story: the same literal title and the same `contracts.Story` exports. |

Worked example: `src/lib/components/rb/browser/RatingStars.*`.

## Rules

- Same DOM: element types, class names, ARIA roles and labels, `title` text and
  attribute order where it shows. Global theme rules (`.perf-root .rb-star` and so on)
  must hit the twin exactly as they hit the original.
- Same behavior: write the contract from the Svelte original first, see it pass there,
  then build the twin until it passes too. Never weaken a contract to fit the twin.
- No new dependencies. Twins import framework-neutral TS from `$lib` (glyphs, math,
  formatters), never a `.svelte` or `.svelte.ts` module. A component that needs a rune
  store waits for that store to be extracted (wave W1).
- Plain `useState` and props only. Per-frame values (playhead, meters) never go through
  React state; they arrive in W1 through the frame clock.
- `-` or `--` for dashes in text and comments, American spelling.

## Checks (all must pass)

```
pnpm check                      # svelte-check + tsc for the .tsx twins
pnpm test:contracts             # every story's play, in Chromium, both renderers
node --experimental-strip-types --test tests/unit/storybook-stories.test.mjs
pnpm build-storybook && pnpm build-storybook:react && pnpm story:parity
```

`story:parity` pixel-diffs every Svelte story against its twin after `play`. It fails a
twin whose story is missing (MISSING) or a React story with no original (ORPHAN), and
reports UNKNOWN (exit 2) when its own controls fail or nothing was compared. Cloud boxes
set `PW_CHROMIUM_PATH=/opt/pw-browsers/chromium-1194/chrome-linux/chrome`.

Browse both side by side: `pnpm storybook` (6006) and `pnpm storybook:react` (6007);
the Svelte Storybook shows the twins under "React twins".
