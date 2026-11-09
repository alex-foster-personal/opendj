// MIXUX (issue #10274): a controller EQ knob in stem-EQ mode drives a stem
// gain, so the pickup target the hardware waits for, and the replica's live
// value, must be that stem gain rather than the plain EQ the knob no longer
// touches. One resolver, `softwareScalar`, serves both callers.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = new URL('../../src/lib/rb/', import.meta.url);
const read = (rel) => readFileSync(new URL(rel, SRC), 'utf8');

const EQ_HIGH = { type: 'mixer_channel', deck: 1, target: 'eq', band: 'high' };
const EQ_LOW = { type: 'mixer_channel', deck: 1, target: 'eq', band: 'low' };

/** @type {Record<string, any>} */
let m;

function readyStems(available) {
	const controls = {};
	for (const stem of ['vocal', 'instrumental', 'drums', 'bass', 'other']) {
		controls[stem] = { muted: false, solo: false, gain: 0.5 };
	}
	return {
		...m.deckStates[1].stems,
		status: 'ready',
		source: 'demucs',
		model: 'htdemucs',
		layout: 'demucs4',
		available_controls: available,
		controls,
		error: null,
		load: null
	};
}

before(async () => {
	m = await loadTypeScriptModule('tests/unit/takeover-engine-sync-under-test.ts');
});

test('stem-EQ mode off: softwareScalar for eq_high is the channel eq_high (pinned)', () => {
	// [if] stem_eq_mode is off [then] the pickup target is the plain EQ, whatever the stems hold.
	const channel = m.mixerState.channels[1];
	channel.stem_eq_mode = false;
	channel.eq_high = 0.62;
	m.deckStates[1].stems = readyStems(['vocal', 'instrumental', 'drums', 'bass']);
	m.deckStates[1].stems.controls.vocal.gain = 0.9;
	assert.equal(m.softwareScalar(EQ_HIGH), 0.62);
	assert.equal(m.eqBandSoftwareValue(1, 'high'), 0.62);
});

test('stem-EQ mode on, stems ready: eq_high resolves to the high-band stem gain and the pickup target follows a stem turn', () => {
	// [if] stem_eq_mode is on and stems are ready [then] eq_high is the vocal gain;
	// [if] that gain changes [then] the hardware picks up against the new gain, not the EQ.
	const channel = m.mixerState.channels[1];
	channel.stem_eq_mode = true;
	channel.eq_high = 0.5;
	m.deckStates[1].stems = readyStems(['vocal', 'instrumental', 'drums', 'bass']);
	m.deckStates[1].stems.controls.vocal.gain = 0.8;
	assert.equal(m.stemForEqBand(1, 'high'), 'vocal');
	assert.equal(m.softwareScalar(EQ_HIGH), 0.8);

	// Knob at 0.2 against a stem gain of 0.8: held (pickup), not applied.
	const held = m.takeoverContinuous(
		EQ_HIGH,
		{ kind: 'continuous', value01: 0.2 },
		'flx4-test',
		'cc:0:7'
	);
	assert.equal(held, null, 'knob far from the stem gain is held for pickup');

	// The stem gain moves (UI, IPC, another controller) to 0.25. The knob
	// sweeping 0.2 -> 0.3 crosses THAT target and picks up. Against the plain
	// EQ (still 0.5) it would stay held.
	m.deckStates[1].stems.controls.vocal.gain = 0.25;
	assert.equal(m.softwareScalar(EQ_HIGH), 0.25);
	const applied = m.takeoverContinuous(
		EQ_HIGH,
		{ kind: 'continuous', value01: 0.3 },
		'flx4-test',
		'cc:0:7'
	);
	assert.equal(applied, 0.3, 'pickup target moved with the stem gain');
});

test('stem-EQ mode on but stems not ready: falls back to the plain EQ', () => {
	// [if] stem_eq_mode is on and stems.status !== ready [then] the plain EQ is the target
	// (the same gate ledTriggerActive and setControllerEq use).
	const channel = m.mixerState.channels[1];
	channel.stem_eq_mode = true;
	channel.eq_high = 0.33;
	m.deckStates[1].stems = {
		...readyStems(['vocal', 'instrumental', 'drums', 'bass']),
		status: 'loading'
	};
	m.deckStates[1].stems.controls.vocal.gain = 0.9;
	assert.equal(m.softwareScalar(EQ_HIGH), 0.33);
});

test('stem-EQ mode on, band with no stem in the bundle: that band stays plain EQ', () => {
	// [if] the bundle has no drums [then] LOW resolves to eq_low, as setControllerEq dispatches it.
	const channel = m.mixerState.channels[1];
	channel.stem_eq_mode = true;
	channel.eq_low = 0.41;
	m.deckStates[1].stems = readyStems(['vocal', 'instrumental']);
	assert.equal(m.stemForEqBand(1, 'low'), null);
	assert.equal(m.softwareScalar(EQ_LOW), 0.41);
});

test('stemForEqBand is the only reader of the one band-to-stem table', () => {
	// [if] a second HI/MID/LOW -> stem table appears, or a caller bypasses stemForEqBand [then] broken.
	const dialMap = read('stem-dial-map.ts');
	const padRuntime = read('midi/controller-pad-runtime.svelte.ts');
	const sync = read('midi/takeover-engine-sync.svelte.ts');

	const table = /have\.has\('vocal'\) \? 'vocal' : null/g;
	assert.equal((dialMap.match(table) ?? []).length, 1, 'the table lives once, in stem-dial-map');
	assert.doesNotMatch(padRuntime, table);
	assert.doesNotMatch(sync, table);

	assert.equal(
		(padRuntime.match(/stemDialAssignment\(/g) ?? []).length,
		1,
		'controller-pad-runtime reads the table only inside stemForEqBand'
	);
	assert.match(padRuntime, /export function stemForEqBand\(deck: DeckId, band: EqDial\)/);
	assert.match(padRuntime, /const stem = stemForEqBand\(deck, band\);/);

	assert.doesNotMatch(sync, /stemDialAssignment/);
	assert.match(
		sync,
		/import \{ stemForEqBand \} from '\$lib\/rb\/midi\/controller-pad-runtime\.svelte';/
	);
	assert.doesNotMatch(
		sync,
		/'vocal'|'instrumental'|'drums'/,
		'no stem literals in the sync module'
	);
});

test('startTakeoverEngineSync notes the stem-aware value for every EQ band', () => {
	// [if] the engine sync still notes channel.eq_* directly [then] pickup ghosts ignore stem-EQ mode.
	const sync = read('midi/takeover-engine-sync.svelte.ts');
	const effect = sync.slice(sync.indexOf('export function startTakeoverEngineSync'));
	assert.match(
		effect,
		/noteTakeoverSoftwareValue\(`mixer:\$\{deck\}:eq:\$\{band\}`, eqBandSoftwareValue\(deck, band\)\)/
	);
	assert.doesNotMatch(effect, /channel\.eq_(high|mid|low)/);
});
