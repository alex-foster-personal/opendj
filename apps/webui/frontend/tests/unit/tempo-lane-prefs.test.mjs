/**
 * Requirements: TEMPOLANE-8342.
 *
 * Tempo-lane prefs: show_tempo_lane (default ON) and tempo_lane_scale
 * (default 'fixed_10'), persisted locally and PUT to /api/v1/ui-prefs the
 * same way STRUCT-05's show_sections is (wave-section-bands.test.mjs).
 *
 * Regression lines:
 * - if show_tempo_lane defaults off then nobody ever sees the lane
 * - if a setter does not PUT its key then an agent and the operator disagree
 * - if a malformed stored value is coerced rather than refused then a typo
 *   silently changes the lane
 * - if the settings catalog or allowlist misses a key then the overlay cannot
 *   drive it
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

let prefsMod;
const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');

before(async () => {
	prefsMod = await loadTypeScriptModule('src/lib/rb/tempo-lane-prefs.ts');
});

describe('tempo lane prefs', () => {
	it('default ON at fixed_10', () => {
		assert.deepEqual(prefsMod.TEMPO_LANE_PREF_DEFAULTS, {
			show_tempo_lane: true,
			tempo_lane_scale: 'fixed_10'
		});
	});

	it('the setters write state, persist and PUT the same key', () => {
		const state = { ...prefsMod.TEMPO_LANE_PREF_DEFAULTS };
		let persisted = 0;
		const patches = [];
		const s = prefsMod.makeTempoLaneSetters(
			state,
			() => (persisted += 1),
			(p) => patches.push(p)
		);
		s.setShowTempoLane(false);
		s.setTempoLaneScale('relative');
		assert.deepEqual(state, { show_tempo_lane: false, tempo_lane_scale: 'relative' });
		assert.equal(persisted, 2);
		assert.deepEqual(patches, [{ show_tempo_lane: false }, { tempo_lane_scale: 'relative' }]);
	});

	it('an unknown scale is refused by the setter, writing nothing', () => {
		const state = { ...prefsMod.TEMPO_LANE_PREF_DEFAULTS };
		const patches = [];
		const s = prefsMod.makeTempoLaneSetters(
			state,
			() => {},
			(p) => patches.push(p)
		);
		assert.throws(() => s.setTempoLaneScale('fixed_5'), /tempo_lane_scale/);
		assert.equal(state.tempo_lane_scale, 'fixed_10');
		assert.deepEqual(patches, []);
	});

	it('absent keys keep the defaults; malformed ones throw naming the storage key', () => {
		assert.deepEqual(prefsMod.validateTempoLanePrefFields({}, 'k'), {});
		assert.deepEqual(
			prefsMod.validateTempoLanePrefFields(
				{ show_tempo_lane: false, tempo_lane_scale: 'relative' },
				'k'
			),
			{ show_tempo_lane: false, tempo_lane_scale: 'relative' }
		);
		assert.throws(
			() => prefsMod.validateTempoLanePrefFields({ show_tempo_lane: 'yes' }, 'mdt.test'),
			/mdt\.test.*show_tempo_lane/
		);
		assert.throws(
			() => prefsMod.validateTempoLanePrefFields({ tempo_lane_scale: 'loud' }, 'mdt.test'),
			/mdt\.test.*tempo_lane_scale/
		);
	});

	it('the settings catalog, allowlist and disk hydrate all name both keys', () => {
		const setting = read('../../src/lib/settings/tempo-lane-setting.ts');
		assert.match(setting, /id: 'show_tempo_lane'/);
		assert.match(setting, /id: 'tempo_lane_scale'/);
		assert.match(read('../../src/lib/settings/catalog.ts'), /\.\.\.TEMPO_LANE_SETTINGS/);
		const apply = read('../../src/lib/settings/apply.ts');
		assert.match(apply, /^\t'show_tempo_lane',$/m);
		assert.match(apply, /^\t'tempo_lane_scale',$/m);
		const hydrate = read('../../src/lib/rb/prefs-hydrate.ts');
		assert.match(hydrate, /uiPrefs\.show_tempo_lane = body\.show_tempo_lane/);
		assert.match(hydrate, /uiPrefs\.tempo_lane_scale = tempoLaneScale/);
	});
});
