import assert from 'node:assert/strict';
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
