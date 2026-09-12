import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/unexpected-pause.ts');
});

const base = {
	autoplay_armed: false,
	context_state: 'running',
	playing: false,
	position_ms: 0,
	duration_ms: 300_000,
	processor_error: null,
	autoplay_handoff_in_flight: false
};

describe('diagnoseUnexpectedPause', () => {
	it('returns null for a command pause', () => {
		assert.equal(mod.diagnoseUnexpectedPause({ ...base, origin: 'command' }), null);
	});

	it('returns null for natural-end with autoplay armed', () => {
		assert.equal(
			mod.diagnoseUnexpectedPause({ ...base, origin: 'natural-end', autoplay_armed: true }),
			null
		);
	});

	it('returns null for natural-end with autoplay off', () => {
		assert.equal(mod.diagnoseUnexpectedPause({ ...base, origin: 'natural-end' }), null);
	});

	it('returns context-suspended when the context is suspended and a deck claims playing', () => {
		assert.equal(
			mod.diagnoseUnexpectedPause({
				...base,
				origin: 'other',
				playing: true,
				context_state: 'suspended'
			}),
			'context-suspended'
		);
	});

	it('returns worklet-error when processor_error is set', () => {
		assert.equal(
			mod.diagnoseUnexpectedPause({ ...base, origin: 'other', processor_error: 'node died' }),
			'worklet-error'
		);
	});

	it('returns autoplay-handoff-failed when a handoff is in flight mid-track', () => {
		assert.equal(
			mod.diagnoseUnexpectedPause({
				...base,
				origin: 'other',
				autoplay_handoff_in_flight: true,
				position_ms: 167_090
			}),
			'autoplay-handoff-failed'
		);
	});

	it('returns source-ended-early for an untagged mid-track stop', () => {
		assert.equal(
			mod.diagnoseUnexpectedPause({
				...base,
				origin: 'other',
				position_ms: 167_090,
				duration_ms: 300_000
			}),
			'source-ended-early'
		);
	});
});

describe('formatUnexpectedPauseMessage', () => {
	it('includes cause and position_ms', () => {
		const message = mod.formatUnexpectedPauseMessage({
			cause: 'source-ended-early',
			deck: 1,
			position_ms: 167_090
		});
		assert.match(message, /cause=source-ended-early/);
		assert.match(message, /position_ms=167090/);
	});
});
