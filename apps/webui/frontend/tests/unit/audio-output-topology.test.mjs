import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const topology = await loadTypeScriptModule('src/lib/rb/audio-output-topology.ts');

test('djio accepts only the named Mixtour Pro four-channel layout', () => {
	assert.equal(topology.parseDjOutputProfile(''), null);
	assert.equal(topology.parseDjOutputProfile('?djio=master12-cue34'), 'master12-cue34');
	assert.throws(
		() => topology.parseDjOutputProfile('?djio=guess'),
		/unsupported profile 'guess'/
	);
});

function monitorState({ master = 0, cue = true, playing = true } = {}) {
	return {
		mixer: {
			master,
			headphones: { level: 0.5, mix: 0 },
			channels: {
				1: { cue_enabled: cue }, 2: { cue_enabled: false },
				3: { cue_enabled: false }, 4: { cue_enabled: false }
			}
		},
		decks: {
			1: { playing, audible: false }, 2: { playing: false, audible: false },
			3: { playing: false, audible: false }, 4: { playing: false, audible: false }
		}
	};
}

test('only an intentionally closed master with a live cue bus suppresses master dropout', () => {
	const cueOnly = monitorState();
	assert.equal(
		topology.cueOnlyMonitoringActive('master12-cue34', cueOnly.mixer, cueOnly.decks),
		true
	);

	const roomAlsoExpected = monitorState({ master: 0.5 });
	assert.equal(
		topology.cueOnlyMonitoringActive(
			'master12-cue34', roomAlsoExpected.mixer, roomAlsoExpected.decks
		),
		false,
		'a cued deck must not hide a real room-output failure while master is raised'
	);

	const noCue = monitorState({ cue: false });
	assert.equal(topology.cueOnlyMonitoringActive('master12-cue34', noCue.mixer, noCue.decks), false);
});

function liveMixer({ trim = 0.5, fader = 1, assign = 'THRU', crossfader = 0.5, master = 0.8 } = {}) {
	const idle = { trim: 0.5, fader: 1, assign: 'THRU', cue_enabled: false };
	return {
		mixer: {
			master,
			crossfader,
			headphones: { level: 0.5, mix: 0 },
			channels: { 1: { trim, fader, assign, cue_enabled: false }, 2: idle, 3: idle, 4: idle }
		},
		decks: {
			1: { playing: true, audible: true }, 2: { playing: false, audible: false },
			3: { playing: false, audible: false }, 4: { playing: false, audible: false }
		}
	};
}

test("a DJ's own mixer move that silences every live deck is not a dropout", () => {
	const held = (options) => {
		const { mixer, decks } = liveMixer(options);
		return topology.mixerHoldsLiveDecksSilent(mixer, decks, null, 1);
	};
	assert.equal(held(), false, 'an open channel keeps the dropout watchdog armed');
	assert.equal(held({ trim: 0 }), true, 'trim at minimum');
	assert.equal(held({ fader: 0 }), true, 'channel fader down');
	assert.equal(held({ master: 0 }), true, 'master volume at zero');
	assert.equal(held({ assign: 'A', crossfader: 1 }), true, 'crossfader parked on the other side');
	assert.equal(held({ assign: 'A', crossfader: 0 }), false, 'crossfader on this side');
	assert.equal(held({ fader: 0.5 }), false, 'a half-open fader is still audible');
});

test('one audible live deck keeps the watchdog armed while another is faded out', () => {
	const { mixer, decks } = liveMixer({ fader: 0 });
	decks[2] = { playing: true, audible: true };
	assert.equal(topology.mixerHoldsLiveDecksSilent(mixer, decks, null, 1), false);
	assert.equal(topology.masterSilenceIsCommanded(null, mixer, decks, null, 1), false);
	mixer.channels[2] = { ...mixer.channels[2], fader: 0 };
	assert.equal(topology.masterSilenceIsCommanded(null, mixer, decks, null, 1), true);
});

test('no live deck means there is no commanded silence to claim', () => {
	const { mixer, decks } = liveMixer({ fader: 0 });
	decks[1] = { playing: false, audible: false };
	assert.equal(topology.mixerHoldsLiveDecksSilent(mixer, decks, null, 1), false);
});

test('a deck routed out by ?extroute= is silent only by its own trim or fader', () => {
	const routed = new Map([[1, 1]]);
	const held = (options) => {
		const { mixer, decks } = liveMixer(options);
		return topology.masterSilenceIsCommanded(null, mixer, decks, routed, 1);
	};
	// The routed deck bypasses the software crossfader and master gain, so the
	// hardware mixer still hears it with either one closed.
	assert.equal(held({ master: 0 }), false, 'master volume does not reach a routed deck');
	assert.equal(held({ assign: 'A', crossfader: 1 }), false, 'the software crossfader does not reach it');
	assert.equal(held({ fader: 0 }), true, 'its own channel fader still silences it');
	assert.equal(held({ trim: 0 }), true, 'its own trim still silences it');
	// Control: the same moves on an unrouted deck are still the DJ's own silence.
	const { mixer, decks } = liveMixer({ master: 0 });
	assert.equal(topology.masterSilenceIsCommanded(null, mixer, decks, new Map([[2, 7]]), 1), true);
	assert.equal(topology.masterSilenceIsCommanded(null, mixer, decks, null, 1), true);
});

test('the calibrated master ceiling counts toward commanded silence, except on a routed deck', () => {
	// METERCAL: M captures a quiet level (-50 dBFS -> ~0.0032) that _masterGain
	// multiplies into the master. A full-open channel under that ceiling is the
	// DJ's own attenuation, not an AUDIO CUT.
	const ceiling = 10 ** (-50 / 20);
	const { mixer, decks } = liveMixer();
	assert.equal(topology.masterSilenceIsCommanded(null, mixer, decks, null, ceiling), true);
	assert.equal(topology.mixerHoldsLiveDecksSilent(mixer, decks, null, ceiling), true);
	// Control: no ceiling (1) keeps an open channel armed, and so does a mild one.
	assert.equal(topology.masterSilenceIsCommanded(null, mixer, decks, null, 1), false);
	assert.equal(topology.masterSilenceIsCommanded(null, mixer, decks, null, 10 ** (-6 / 20)), false);
	// A routed deck skips _masterGain, so the ceiling does not silence it.
	assert.equal(topology.masterSilenceIsCommanded(null, mixer, decks, new Map([[1, 1]]), ceiling), false);
});

test('the engine passes its active ceiling to the silence check', () => {
	const engine = readFileSync(new URL('../../src/lib/rb/audio-engine.svelte.ts', import.meta.url), 'utf8');
	assert.match(engine, /masterSilenceIsCommanded\([^)]*_externalRouting, _ceilingGainMultiplier\(\)\)/);
	assert.match(engine, /masterBusSilenceIsCommanded\([^)]*_externalRouting, _ceilingGainMultiplier\(\)\)/);
	assert.match(engine, /noteMasterSilence\(_masterAnalyser, _externalRouteAnalyser, anyTransport, Date\.now\(\), intentionalSilence, busSilence\)/);
});

test('routed-only playback leaves the master bus silent by design, not the output', () => {
	// Codex P1, PR #6829: a routed deck never feeds _masterGain, the one tap the
	// bus watchdog reads, so its open fader must not arm an AUDIO CUT there.
	const routed = new Map([[1, 1]]);
	const { mixer, decks } = liveMixer();
	assert.equal(topology.masterBusSilenceIsCommanded(null, mixer, decks, routed, 1), true, 'an unfed bus raised a dropout');
	assert.equal(topology.masterSilenceIsCommanded(null, mixer, decks, routed, 1), false, 'device liveness lost the routed deck');
	// Overshoot control: an unrouted live deck with its fader open still arms the bus.
	decks[2] = { playing: true, audible: true };
	mixer.channels[2] = { ...mixer.channels[1] };
	assert.equal(topology.masterBusSilenceIsCommanded(null, mixer, decks, routed, 1), false, 'a live bus deck lost its watchdog');
	mixer.channels[2] = { ...mixer.channels[2], fader: 0 };
	assert.equal(topology.masterBusSilenceIsCommanded(null, mixer, decks, routed, 1), true);
	// With nothing routed the two checks agree.
	const plain = liveMixer();
	assert.equal(topology.masterBusSilenceIsCommanded(null, plain.mixer, plain.decks, null, 1), false);
	assert.equal(topology.masterBusSilenceIsCommanded(null, plain.mixer, plain.decks, new Map(), 1), false);
});

test('cue-only monitoring does not excuse a live routed deck with its own path open', () => {
	// Sol P1, PR #6829: master closed + cue on makes cueOnlyMonitoringActive
	// true, but a ?extroute= deck reaches its own output past master and cue.
	const { mixer, decks } = liveMixer({ master: 0 });
	mixer.headphones = { level: 0.5, mix: 0 };
	mixer.channels[1] = { ...mixer.channels[1], cue_enabled: true };
	assert.equal(topology.cueOnlyMonitoringActive('master12-cue34', mixer, decks), true);
	const routed = new Map([[1, 1]]);
	assert.equal(
		topology.masterSilenceIsCommanded('master12-cue34', mixer, decks, routed, 1),
		false,
		'cue-only hid an audible routed deck from device liveness'
	);
	// Overshoot controls: its own fader still silences it, and with nothing
	// routed cue-only monitoring still explains the closed master.
	mixer.channels[1] = { ...mixer.channels[1], fader: 0 };
	assert.equal(topology.masterSilenceIsCommanded('master12-cue34', mixer, decks, routed, 1), true);
	mixer.channels[1] = { ...mixer.channels[1], fader: 1 };
	assert.equal(topology.masterSilenceIsCommanded('master12-cue34', mixer, decks, null, 1), true);
	// A routed deck that is not playing does not count either.
	decks[1] = { playing: false, audible: false };
	decks[2] = { playing: true, audible: true };
	mixer.channels[2] = { ...mixer.channels[2], cue_enabled: true };
	assert.equal(topology.masterSilenceIsCommanded('master12-cue34', mixer, decks, routed, 1), true);
});

test('the silence source gate sees the master bus the watchdog measures: routing and the ceiling count', async () => {
	// Codex P1, PR #6829: the source gate scaled each deck's PCM by onAirGain
	// alone, so a routed deck (which never feeds the internal master bus) and
	// the calibrated ceiling's attenuation were both counted as expected output,
	// and the watchdog stopped audible decks for a "dropout" that was commanded.
	const pcm = await loadTypeScriptModule('src/lib/rb/silence-source-pcm.ts');
	const { SILENCE_RMS_FLOOR } = await loadTypeScriptModule('src/lib/rb/silence-watchdog.ts');
	const buffer = (fill) => ({
		sampleRate: 1000,
		length: 2000,
		numberOfChannels: 1,
		getChannelData: () => new Float32Array(2000).fill(fill)
	});
	const { mixer } = liveMixer();
	const snap = (deck, fill, routed, ceiling = 1) => ({
		claims_live: true,
		buffer: buffer(fill),
		position_sec: 0,
		master_path_gain: topology.masterBusPathGain(mixer, deck, routed, ceiling)
	});

	// A loud routed deck next to an open unrouted deck in its silent intro: the
	// bus is quiet by design, so the source reads silent too.
	const routed = new Map([[1, 1]]);
	assert.equal(topology.masterBusPathGain(mixer, 1, routed, 1), 0, 'a routed deck was counted on the master bus');
	assert.equal(pcm.claimedLiveSourceIsSilent([snap(1, 0.5, routed), snap(2, 0, routed)]), true);
	// Control: the same loud deck unrouted does feed the bus.
	assert.equal(pcm.claimedLiveSourceIsSilent([snap(1, 0.5, null), snap(2, 0, null)]), false);

	// A moderate ceiling (above the commanded-silence threshold) still scales
	// what the bus can carry.
	const open = topology.masterBusPathGain(mixer, 1, null, 1);
	assert.ok(open > 0);
	const fill = (2 * SILENCE_RMS_FLOOR) / open; // twice the floor through an open master
	assert.equal(pcm.claimedLiveSourceIsSilent([snap(1, fill, null, 1)]), false, 'control: no ceiling is audible');
	assert.equal(pcm.claimedLiveSourceIsSilent([snap(1, fill, null, 0.1)]), true, 'the ceiling was left out of the gain');
	assert.ok(Math.abs(topology.masterBusPathGain(mixer, 1, null, 0.1) - open * 0.1) < 1e-12);

	// Wiring: the watchdog's source reader takes this gain from the engine,
	// with the live routing map and ceiling, not from onAirGain.
	const appInit = readFileSync(new URL('../../src/lib/rb/app-init.ts', import.meta.url), 'utf8');
	assert.match(appInit, /master_path_gain: deckMasterBusGain\(id\)/);
	assert.doesNotMatch(appInit, /onAirGain\(/);
	const engine = readFileSync(new URL('../../src/lib/rb/audio-engine.svelte.ts', import.meta.url), 'utf8');
	assert.match(
		engine,
		/export function deckMasterBusGain\(deck: DeckId\): number \{\s*return masterBusPathGain\(mixerState, deck, _externalRouting, _ceilingGainMultiplier\(\)\) \* autoGainLinear\(deck\);/
	);
});

test('a deck a partial ?extroute= map leaves out never arms output-device liveness', () => {
	// Sol P1, PR #6829: under an active map only the external merger reaches the
	// device, so an open unmapped deck is heard nowhere and must not keep device
	// liveness armed against a silent routed tap.
	const routed = new Map([[2, 1]]);
	const { mixer, decks } = liveMixer();
	assert.equal(topology.masterSilenceIsCommanded(null, mixer, decks, routed, 1), true, 'an unmapped deck armed device liveness');
	// It still feeds the internal bus, so the bus watchdog stays armed for it.
	assert.equal(topology.masterBusSilenceIsCommanded(null, mixer, decks, routed, 1), false, 'the bus lost its unmapped deck');
	assert.ok(topology.masterBusPathGain(mixer, 1, routed, 1) > 0);
	// Overshoot controls: a live mapped deck still arms the output, and with no
	// map (null or empty) the same open deck is an ordinary output path.
	decks[2] = { playing: true, audible: true };
	mixer.channels[2] = { ...mixer.channels[1] };
	assert.equal(topology.masterSilenceIsCommanded(null, mixer, decks, routed, 1), false, 'a mapped deck lost device liveness');
	const plain = liveMixer();
	assert.equal(topology.masterSilenceIsCommanded(null, plain.mixer, plain.decks, null, 1), false);
	assert.equal(topology.masterSilenceIsCommanded(null, plain.mixer, plain.decks, new Map(), 1), false);
});
