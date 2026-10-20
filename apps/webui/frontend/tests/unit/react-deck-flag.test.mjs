/**
 * requirement: REACT-01
 *
 * `ui.react.deck` (default OFF) swaps the deck's leaf controls for their React
 * twins. It FAILS CLOSED, the opposite of `sets.recording`: only a flag the
 * daemon DECLARES on mounts React; unread, undeclared or off keeps Svelte.
 *
 * [if] the flags are unread, undeclared or the flag is off [then] the deck
 *      renders the Svelte originals, unchanged, [else stop].
 * [if] the daemon declares the flag on [then] each gated leaf renders a
 *      ReactIsland host instead of the Svelte markup, [else stop].
 * [if] a twin needs a per-frame value (BeatJump reads position_ms) [then] it
 *      stays on Svelte with the flag on too, [else stop].
 *
 * The mount itself is proven in real Chromium by the ReactIsland contract
 * stories (src/lib/react/ReactIsland.stories.ts). Deck.svelte cannot be
 * SSR-rendered under node (its children run rune modules this harness does
 * not compile), so the branch structure is pinned from the source here.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { reactDeckEnabledIn, REACT_DECK_FLAG_ID } = await loadTypeScriptModule('src/lib/react/react-deck-flag.ts');

const read = (path) => readFileSync(new URL(`../../src/${path}`, import.meta.url), 'utf8');

test('the React deck shows only when the daemon declares ui.react.deck on', () => {
	assert.equal(REACT_DECK_FLAG_ID, 'ui.react.deck');
	assert.equal(reactDeckEnabledIn([]), false, 'flags unread: Svelte');
	assert.equal(reactDeckEnabledIn([{ flag_id: 'sets.recording', enabled: true }]), false, 'another flag on changes nothing');
	assert.equal(reactDeckEnabledIn([{ flag_id: 'ui.react.deck', enabled: false }]), false);
	assert.equal(reactDeckEnabledIn([{ flag_id: 'ui.react.deck', enabled: true }]), true);
});

// The loaders name the `.tsx` file explicitly: Vite resolves an extensionless
// specifier to the Svelte sibling of the same name first, which would mount
// the Svelte component inside ReactIsland and throw.
test('every gated leaf has both branches, keyed on the one flag read', () => {
	const deck = read('lib/components/rb/Deck.svelte');
	assert.match(deck, /const reactDeck: boolean = \$derived\(reactDeckEnabled\(\)\);/);
	for (const twin of ['PadStrip', 'PadModeMenu', 'StemRow', 'DeckErrorBanner', 'SecondaryLoadBadge']) {
		assert.match(deck, new RegExp(`<ReactIsland\\s+load=\\{\\(\\) => import\\('\\./deck/${twin}\\.tsx'\\)\\}`), twin);
		assert.match(deck, new RegExp(`<${twin}\\b`), `${twin} keeps its Svelte branch`);
	}
	assert.doesNotMatch(deck, /import\('\.\/deck\/BeatJump(\.tsx)?'\)/, 'BeatJump must not go through React');
	const bank = read('lib/components/rb/deck/HotCueBank.svelte');
	assert.match(bank, /\{#if reactDeck\}\s*<ReactIsland load=\{\(\) => import\('\.\/HotCueProposalLabel\.tsx'\)\} props=\{\{ kind: visible\.kind \}\} \/>\s*\{:else\}\s*<HotCueProposalLabel kind=\{visible\.kind\} \/>/);
});

// Same rule for the island's own stories: an extensionless loader there
// would hand the Svelte sibling to React and prove nothing about the twin.
test('ReactIsland stories load twins by their .tsx file', () => {
	const stories = read('lib/react/ReactIsland.stories.ts');
	const loads = [...stories.matchAll(/import\('(\$lib\/components\/[^']+)'\)/g)].map((m) => m[1]);
	assert.ok(loads.length >= 3, `found ${loads.length} twin loaders`);
	for (const spec of loads) assert.match(spec, /\.tsx$/, spec);
});
