/**
 * House rule: a switch over a closed union ends in a `_exhaustive: never`
 * default that THROWS, so adding a union member turns the compiler red and
 * an unmodeled runtime value turns loud instead of silently labeling
 * nothing. Issue #724 converts the last two switches that swallowed unknown
 * values as `return null`.
 *
 * These are runtime tests on purpose. The compile-time half is enforced by
 * svelte-check; only a real call can prove the swallow is gone, so every
 * assertion below drives the shipped function with a value cast past the
 * type system, exactly as a server that grew a fifth enum member would.
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let apiRb;
let bpmHeat;

before(async () => {
	apiRb = await loadTypeScriptModule('src/lib/rb/api-rb.ts');
	bpmHeat = await loadTypeScriptModule('src/lib/rb/bpm-heat.ts');
});

describe('artworkStatusLabel exhaustiveness', () => {
	it('labels every modeled failure status', () => {
		const { artworkStatusLabel } = apiRb;
		assert.equal(artworkStatusLabel('no_image_path'), 'no artwork path in library');
		assert.equal(artworkStatusLabel('unresolved'), 'artwork path unresolved');
		assert.equal(artworkStatusLabel('file_missing'), 'artwork file missing');
	});

	it('returns null for ok and for an absent status', () => {
		const { artworkStatusLabel } = apiRb;
		// null / undefined are MEMBERS of the parameter type, not unknown
		// values. The backend RbMetaOut does not send artwork_status at all
		// (apps/webui/server/routes/rb_assets.py), so every live TrackTable
		// row reaches this function as undefined; throwing here would blank
		// the browser on load.
		assert.equal(artworkStatusLabel('ok'), null);
		assert.equal(artworkStatusLabel(null), null);
		assert.equal(artworkStatusLabel(undefined), null);
	});

	it('throws on an unmodeled status rather than returning null', () => {
		const { artworkStatusLabel } = apiRb;
		assert.throws(
			() => artworkStatusLabel('permission_denied'),
			/unhandled artwork status: permission_denied/
		);
	});
});

describe('bpmHeatLabel exhaustiveness', () => {
	it('labels every modeled lane', () => {
		const { bpmHeatLabel } = bpmHeat;
		const heat = (lane, fold) => ({ lane, fold, absDelta: 0, relDelta: 0, color: 'rgb(0 0 0)' });
		assert.match(bpmHeatLabel(heat('sweet', 1), 128), /within 6% of master 128\.0/);
		assert.match(bpmHeatLabel(heat('half', 0.5), 128), /^half-tempo of master 128\.0/);
		assert.match(bpmHeatLabel(heat('half', 2), 128), /^double-tempo of master 128\.0/);
		assert.match(bpmHeatLabel(heat('mid', 1), 128), /mid-range/);
		assert.match(bpmHeatLabel(heat('far', 1), 128), /far from master/);
	});

	it('still returns null for no heat or no master', () => {
		const { bpmHeatLabel } = bpmHeat;
		assert.equal(bpmHeatLabel(null, 128), null);
		assert.equal(
			bpmHeatLabel({ lane: 'sweet', fold: 1, absDelta: 0, relDelta: 0, color: '' }, null),
			null
		);
	});

	it('throws on an unmodeled lane rather than returning null', () => {
		const { bpmHeatLabel } = bpmHeat;
		assert.throws(
			() => bpmHeatLabel({ lane: 'nuclear', fold: 1, absDelta: 0, relDelta: 0, color: '' }, 128),
			/unhandled BPM heat lane: nuclear/
		);
	});

	it('every lane classifyBpmHeat can emit is labeled', () => {
		const { classifyBpmHeat, bpmHeatLabel } = bpmHeat;
		// The producer is the only real caller, so prove the pair agree: no
		// classification it emits may reach the throwing default.
		const cases = [
			[128, 128],
			[90, 180],
			[256, 128],
			[140, 128],
			[100, 128],
			[160, 128]
		];
		for (const [bpm, master] of cases) {
			const heat = classifyBpmHeat(bpm, master);
			assert.ok(heat !== null, `expected heat for ${bpm} vs ${master}`);
			assert.equal(typeof bpmHeatLabel(heat, master), 'string');
		}
	});
});
