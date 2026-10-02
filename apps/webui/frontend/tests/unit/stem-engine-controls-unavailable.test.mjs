// requirement: a deck with no stem bundle ('unavailable') is not an error.
//
// [if] applyStemControl is called for a deck whose stems.status is
//   'unavailable' [then] it must no-op silently (no throw, no state
//   mutation, no processor call) rather than raise the "deck N stems are
//   unavailable" error that lit up the persistent red DeckErrorBanner for
//   the normal, permanent state of any track without stems.
// [if] applyStemControl is called for a deck whose stems.status is
//   'loading' or 'error' [then] it must still throw - those are the real,
//   loud failure paths and must not be silenced by this fix.
// [if] requireLoaded throws "no track loaded" [then] applyStemControl no-ops.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let controlsModule;

before(async () => {
	controlsModule = await loadTypeScriptModule('src/lib/rb/stem-engine-controls.ts');
});

function stemControls() {
	return {
		vocal: { muted: false, solo: false, gain: 0.5 },
		instrumental: { muted: false, solo: false, gain: 0.5 },
		drums: { muted: false, solo: false, gain: 0.5 }
	};
}

function deckWithStemStatus(status, error = null) {
	return {
		stems: {
			status,
			source: null,
			model: null,
			layout: null,
			available_controls: [],
			alignment: null,
			controls: stemControls(),
			error
		}
	};
}

function depsFor(st, rt = { processor: null }) {
	return {
		requireLoaded: () => ({ st, rt }),
		getChannel: () => ({})
	};
}

test('applyStemControl no-ops for an unavailable deck instead of throwing', () => {
	const st = deckWithStemStatus('unavailable', 'no stem bundle advertised');
	const before = stemControls();
	st.stems.controls = before;
	const rt = { processor: null };

	assert.doesNotThrow(() =>
		controlsModule.applyStemControl(1, 'vocal', 'muted', true, depsFor(st, rt))
	);

	// No mutation: controls object is untouched and no processor call happened.
	assert.equal(st.stems.controls, before);
	assert.equal(st.stems.controls.vocal.muted, false);
});

test('applyStemControl no-ops on solo and gain fields too for an unavailable deck', () => {
	const st = deckWithStemStatus('unavailable');
	assert.doesNotThrow(() =>
		controlsModule.applyStemControl(2, 'drums', 'solo', true, depsFor(st))
	);
	assert.doesNotThrow(() =>
		controlsModule.applyStemControl(2, 'instrumental', 'gain', 0.9, depsFor(st))
	);
	assert.equal(st.stems.controls.drums.solo, false);
	assert.equal(st.stems.controls.instrumental.gain, 0.5);
});

test('applyStemControl still throws for a loading deck', () => {
	const st = deckWithStemStatus('loading');
	assert.throws(
		() => controlsModule.applyStemControl(3, 'vocal', 'muted', true, depsFor(st)),
		/deck 3 stems are loading/
	);
});

function unloadedDeps(deck) {
	return {
		requireLoaded: (_deck, op) => {
			throw new Error(`${op}: no track loaded on deck ${deck}`);
		},
		getChannel: () => {
			throw new Error('getChannel must not run for an unloaded deck');
		}
	};
}

test('applyStemControl no-ops when no track is loaded instead of throwing', () => {
	assert.doesNotThrow(() =>
		controlsModule.applyStemControl(1, 'vocal', 'muted', true, unloadedDeps(1))
	);
	assert.doesNotThrow(() =>
		controlsModule.applyStemControl(1, 'drums', 'solo', true, unloadedDeps(1))
	);
	assert.doesNotThrow(() =>
		controlsModule.applyStemControl(1, 'instrumental', 'gain', 0.25, unloadedDeps(1))
	);
});

test('applyStemControl still throws a mismatched no-track-loaded error', () => {
	const deps = {
		requireLoaded: () => {
			throw new Error('setStemMute: no track loaded on deck 2');
		},
		getChannel: () => ({})
	};
	assert.throws(
		() => controlsModule.applyStemControl(1, 'vocal', 'muted', true, deps),
		/no track loaded on deck 2/
	);
});

test('applyStemControl still throws for an error deck, with the reason', () => {
	const st = deckWithStemStatus('error', 'stem/source alignment mismatch');
	assert.throws(
		() => controlsModule.applyStemControl(4, 'vocal', 'muted', true, depsFor(st)),
		/deck 4 stems are error: stem\/source alignment mismatch/
	);
});
