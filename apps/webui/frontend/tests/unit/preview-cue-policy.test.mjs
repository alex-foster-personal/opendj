/**
 * Requirements: CUEOUT-15.
 *
 * The library preview must never be started into something the operator
 * cannot hear, and must say which of the several possible causes applies.
 *
 * Regression lines:
 * - if a preview is reported audible while the gain on its route is 0 then the
 *   operator clicks a waveform, hears nothing, and has no way to tell a broken
 *   feature from a MIX knob at the master end
 * - if a selected but dead cue sink is treated as audible then the preview is
 *   started into a device that is not playing, which is the exact
 *   silent-pass this repo keeps writing autopsies about
 * - if practice mode reports no warning then a preview goes out of the ROOM
 *   speakers mid-set with no notice
 * - if a page that has never opened the I/O menu is refused then the feature is
 *   unreachable in practice mode, which needs no device selection at all
 * - if the floor is applied with >= rather than > 0 then a genuinely silent
 *   path is downgraded from a refusal to a warning
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let previewCueVerdict;
let AUDIBLE_FLOOR;

const HEALTHY = {
	graph_built: true,
	active: true,
	output_mode: 'two_outputs',
	cue_device_label: 'Scarlett 2i2',
	cue_path_gain: 0.8
};

before(async () => {
	const mod = await loadTypeScriptModule('src/lib/player/preview-cue-policy.ts');
	previewCueVerdict = mod.previewCueVerdict;
	AUDIBLE_FLOOR = mod.AUDIBLE_FLOOR;
});

describe('preview cue policy', () => {
	it('a healthy two-output rig is audible on the cue route with no warning', () => {
		const v = previewCueVerdict({ ...HEALTHY });
		assert.equal(v.audible, true);
		assert.equal(v.route, 'cue');
		assert.equal(v.warning, null);
	});

	it('no audio graph is refused, naming what to do about it', () => {
		const v = previewCueVerdict({ ...HEALTHY, graph_built: false });
		assert.equal(v.audible, false);
		assert.match(v.refusal, /audio engine is not running/);
	});

	it('a page that never enumerated devices still previews in practice mode', () => {
		const v = previewCueVerdict({
			...HEALTHY,
			output_mode: 'practice',
			active: false,
			cue_device_label: null,
			cue_path_gain: 0.5
		});
		assert.equal(v.audible, true);
		assert.equal(v.route, 'main_practice');
	});

	it('a selected but dead cue sink is refused and named', () => {
		const v = previewCueVerdict({ ...HEALTHY, active: false });
		assert.equal(v.audible, false);
		assert.match(v.refusal, /Scarlett 2i2/);
	});

	it('a dead sink with no label still refuses, without printing null', () => {
		const v = previewCueVerdict({ ...HEALTHY, active: false, cue_device_label: null });
		assert.equal(v.audible, false);
		assert.match(v.refusal, /the selected cue device/);
		assert.ok(!v.refusal.includes('null'));
	});

	it('a zero-gain cue path is REFUSED, not started silently', () => {
		const v = previewCueVerdict({ ...HEALTHY, cue_path_gain: 0 });
		assert.equal(v.audible, false);
		assert.match(v.refusal, /MIX/);
	});

	it('a gain under the floor plays but warns which knob is at fault', () => {
		const v = previewCueVerdict({ ...HEALTHY, cue_path_gain: AUDIBLE_FLOOR / 2 });
		assert.equal(v.audible, true);
		assert.match(v.warning, /very quiet/);
		assert.match(v.warning, /MIX/);
	});

	it('exactly the floor is not "very quiet" (the boundary bites from both sides)', () => {
		const at = previewCueVerdict({ ...HEALTHY, cue_path_gain: AUDIBLE_FLOOR });
		assert.equal(at.warning, null);
		const under = previewCueVerdict({ ...HEALTHY, cue_path_gain: AUDIBLE_FLOOR - 1e-9 });
		assert.match(under.warning, /very quiet/);
	});

	it('practice mode is audible on MAIN and says so', () => {
		const v = previewCueVerdict({
			...HEALTHY,
			output_mode: 'practice',
			active: false,
			cue_device_label: null,
			cue_path_gain: 0.5
		});
		assert.equal(v.audible, true);
		assert.equal(v.route, 'main_practice');
		assert.match(v.warning, /MAIN output/);
	});

	it('practice mode does NOT require an active monitor (that check is two_outputs only)', () => {
		const v = previewCueVerdict({ ...HEALTHY, output_mode: 'practice', active: false });
		assert.equal(v.audible, true);
	});

	it('split cable routes to the right ear', () => {
		const v = previewCueVerdict({ ...HEALTHY, output_mode: 'split_cable', active: false });
		assert.equal(v.audible, true);
		assert.equal(v.route, 'split_right');
		assert.equal(v.warning, null);
	});

	it('a nonsense gain throws rather than guessing', () => {
		assert.throws(() => previewCueVerdict({ ...HEALTHY, cue_path_gain: Number.NaN }), RangeError);
		assert.throws(() => previewCueVerdict({ ...HEALTHY, cue_path_gain: -1 }), RangeError);
	});

	it('an unknown output mode throws rather than defaulting to a route', () => {
		assert.throws(() => previewCueVerdict({ ...HEALTHY, output_mode: 'quadraphonic' }), /output mode/);
	});
});
