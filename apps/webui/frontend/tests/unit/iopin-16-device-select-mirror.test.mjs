// requirement: IOPIN-16
// [if] a device pick settles and the router holds a different device (refused, rolled back, or redirected to split L/R) [then] the select shows what the router holds, [else stop]
// [if] the router took the pick [then] the select keeps showing the picked device, [else stop]
// [if] the pick throws [then] the select still mirrors the held device, [else stop]
// [if] MAIN and CUE are one output (CUEOUT-26 split) [then] the CUE placeholder names where the cue went, not "HP out", [else stop]
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const CLUSTER = `${FRONTEND}/src/lib/components/rb/mixer/HeadphoneCluster.svelte`;

let mirror;
before(async () => {
	mirror = await loadTypeScriptModule('src/lib/components/rb/mixer/device-select-mirror.ts');
});

test('a CUE pick redirected to split L/R stops showing the picked headphones (the maintainer, Thu 8 Oct 2026)', async () => {
	// The reproduced sequence: MAIN = External Headphones, then CUE = External
	// Headphones. CUEOUT-26 runs split L/R and holds NO separate CUE, so the
	// bound value stays '' and Svelte never rewrites the element.
	const state = { cue: null };
	const select = { value: 'hp-id' };
	const picked = [];
	await mirror.pickThenMirror(select, async (id) => { picked.push(id); state.cue = null; }, () => state.cue);
	assert.deepEqual(picked, ['hp-id'], 'the clicked device is what the router was asked for');
	assert.equal(select.value, '', 'the select shows the held (empty) CUE, not the clicked headphones');
});

test('control: a pick the router took keeps showing the picked device', async () => {
	const state = { cue: null };
	const select = { value: 'hp-id' };
	await mirror.pickThenMirror(select, async (id) => { state.cue = id; }, () => state.cue);
	assert.equal(select.value, 'hp-id');
});

test('a failed pick rolls the select back to the device still held', async () => {
	const state = { master: 'spk-id' };
	const select = { value: 'hp-id' };
	await assert.rejects(
		mirror.pickThenMirror(select, async () => { throw new Error('device busy'); }, () => state.master),
		/device busy/
	);
	assert.equal(select.value, 'spk-id');
});

test('every device select in the I/O panel goes through the mirror', () => {
	const source = readFileSync(CLUSTER, 'utf8');
	for (const [label, held] of [
		['master output device', 'selected_master_output_device_id'],
		['headphone output device', 'selected_output_device_id'],
		['audio input device', 'selected_input_device_id']
	]) {
		const at = source.indexOf(`aria-label="${label}"`);
		assert.ok(at > 0, `${label} select is missing`);
		const tag = source.slice(at, source.indexOf('</select>', at));
		assert.match(tag, new RegExp(`pickThenMirror\\(event\\.currentTarget, \\w+, \\(\\) => headphoneState\\.${held}\\)`), `${label} must mirror ${held}`);
	}
});

test('in a same-device split the CUE placeholder says the cue is the right ear of MAIN', () => {
	const source = readFileSync(CLUSTER, 'utf8');
	assert.match(source, /split_reason === 'same_device'[^\n]*\n[^\n]*right ear of MAIN \(split L\/R\)/);
	assert.match(source, /<option value="" disabled>\{cuePlaceholder\}<\/option>/);
	assert.doesNotMatch(source, />HP out</);
});
