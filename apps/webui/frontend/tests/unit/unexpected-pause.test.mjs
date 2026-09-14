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

const RAW_DIAGNOSTIC_TOKENS = [/cause=/, /position_ms=/, /decoded_duration_ms=/, /metadata_duration_ms=/];

function assertHumanMessage(message) {
	for (const token of RAW_DIAGNOSTIC_TOKENS) {
		assert.doesNotMatch(message, token, `human toast must not contain ${token}`);
	}
}

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

describe('formatMsAsClock', () => {
	it('floors fractional seconds and zero-pads', () => {
		assert.equal(mod.formatMsAsClock(29_692), '0:29');
	});

	it('renders minute boundaries', () => {
		assert.equal(mod.formatMsAsClock(60_000), '1:00');
	});
});

describe('formatUnexpectedPauseDiagnostic', () => {
	it('preserves raw cause, deck, and position fields', () => {
		const message = mod.formatUnexpectedPauseDiagnostic({
			cause: 'source-ended-early',
			deck: 2,
			position_ms: 29_692,
			decoded_duration_ms: 29_700,
			metadata_duration_ms: 252_000
		});
		assert.match(message, /cause=source-ended-early/);
		assert.match(message, /deck=2/);
		assert.match(message, /position_ms=29692/);
		assert.match(message, /decoded_duration_ms=29700/);
		assert.match(message, /metadata_duration_ms=252000/);
	});

	it('records null metadata duration for source-ended-early', () => {
		const message = mod.formatUnexpectedPauseDiagnostic({
			cause: 'source-ended-early',
			deck: 1,
			position_ms: 10_000,
			decoded_duration_ms: 30_000,
			metadata_duration_ms: null
		});
		assert.match(message, /metadata_duration_ms=null/);
	});

	it('includes context state when present', () => {
		const message = mod.formatUnexpectedPauseDiagnostic({
			cause: 'context-suspended',
			deck: 1,
			position_ms: 1_000,
			context_state: 'suspended'
		});
		assert.match(message, /state=suspended/);
	});
});

describe('formatUnexpectedPauseMessage', () => {
	it('formats source-ended-early with deck, stop time, and listed end', () => {
		const message = mod.formatUnexpectedPauseMessage({
			cause: 'source-ended-early',
			deck: 2,
			position_ms: 29_692,
			decoded_duration_ms: 29_700,
			metadata_duration_ms: 252_000
		});
		assert.equal(
			message,
			"Deck 2 stopped at 0:29, before the track's listed end (4:12). The audio file may be cut short."
		);
		assertHumanMessage(message);
	});

	it('labels decoded duration when metadata is unavailable', () => {
		const message = mod.formatUnexpectedPauseMessage({
			cause: 'source-ended-early',
			deck: 1,
			position_ms: 29_692,
			decoded_duration_ms: 30_000,
			metadata_duration_ms: null
		});
		assert.equal(
			message,
			'Deck 1 stopped at 0:29, before the decoded audio ends (0:30). The audio file may be cut short.'
		);
		assertHumanMessage(message);
	});

	it('states listed length is unavailable when both durations are missing', () => {
		const message = mod.formatUnexpectedPauseMessage({
			cause: 'source-ended-early',
			deck: 2,
			position_ms: 29_692
		});
		assert.match(message, /listed length is unavailable/);
		assert.doesNotMatch(message, /listed end/);
		assertHumanMessage(message);
	});

	it('formats context-suspended without diagnostic tokens', () => {
		const message = mod.formatUnexpectedPauseMessage({
			cause: 'context-suspended',
			deck: 3,
			position_ms: 45_000,
			context_state: 'suspended'
		});
		assert.match(message, /Deck 3/);
		assert.match(message, /interrupted/);
		assertHumanMessage(message);
	});

	it('formats worklet-error without diagnostic tokens', () => {
		const message = mod.formatUnexpectedPauseMessage({
			cause: 'worklet-error',
			deck: 4,
			position_ms: 12_345
		});
		assert.match(message, /Deck 4/);
		assert.match(message, /processor failed/);
		assertHumanMessage(message);
	});

	it('formats autoplay-handoff-failed without diagnostic tokens', () => {
		const message = mod.formatUnexpectedPauseMessage({
			cause: 'autoplay-handoff-failed',
			deck: 1,
			position_ms: 167_090
		});
		assert.match(message, /Deck 1/);
		assert.match(message, /AutoPlay/);
		assertHumanMessage(message);
	});
});
