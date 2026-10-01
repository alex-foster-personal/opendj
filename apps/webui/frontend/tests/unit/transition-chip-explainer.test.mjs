/**
 * @pytest.mark.requirement TRANS-02
 * Pin 9c2caa34455f: the TopBar blend pill explains itself on hover.
 *
 * [if] the pill reads transitioning [then] its hover text says two decks are
 *   audible on the master and names both decks
 * [if] the pill reads approaching [then] its hover text says one deck is
 *   audible and names the deck that looks about to come in
 * [if] the state is idle [then] there is no hover text (the pill is hidden)
 * [if] the pill is hovered [then] the whole pill carries the text, and a
 *   press on it cannot move keyboard focus off the decks
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const mod = await loadTypeScriptModule('src/lib/rb/transition-chip-explainer.ts');
const CHIP = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/TransitioningChip.svelte', import.meta.url)),
	'utf8'
);

test('transitioning names both decks and what audible means', () => {
	const text = mod.transitionChipTitle({
		state: 'transitioning',
		outgoing_deck: 1,
		incoming_deck: 2
	});
	assert.match(text, /two decks/i);
	assert.match(text, /master/i);
	assert.match(text, /deck 1/i);
	assert.match(text, /deck 2/i);
});

test('approaching names the outgoing and the incoming deck', () => {
	const text = mod.transitionChipTitle({
		state: 'approaching',
		outgoing_deck: 3,
		incoming_deck: 4
	});
	assert.match(text, /deck 3/i);
	assert.match(text, /deck 4/i);
	assert.match(text, /about to/i);
	assert.doesNotMatch(text, /two decks are audible/i);
});

test('both live states say the pill is status only with no settings', () => {
	for (const state of ['approaching', 'transitioning']) {
		const text = mod.transitionChipTitle({ state, outgoing_deck: 1, incoming_deck: 2 });
		assert.match(text, /status only/i);
		assert.match(text, /no settings/i);
	}
});

test('idle has no hover text', () => {
	assert.equal(
		mod.transitionChipTitle({ state: 'idle', outgoing_deck: null, incoming_deck: null }),
		''
	);
});

test('the whole pill carries the hover text and cannot take focus', () => {
	assert.match(CHIP, /title=\{chipTitle\}/);
	assert.match(CHIP, /transitionChipTitle\(status\)/);
	assert.match(CHIP, /onmousedown=\{keepDeckFocus\}/);
	assert.match(CHIP, /\.transition-chip \{[^}]*pointer-events:\s*auto/);
	assert.doesNotMatch(CHIP, /<button/);
	assert.doesNotMatch(CHIP, /tabindex/);
});
