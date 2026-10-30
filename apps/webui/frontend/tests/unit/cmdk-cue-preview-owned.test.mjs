/**
 * Sol P1 on #6687: a refused jump sends the deferred preview_stop only for a
 * source this controller owns. A jump queued behind a first start still in
 * flight owns nothing, so a refusal after Escape must not stop a newer preview
 * that another dispatcher client started.
 */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { createCuePreview, MASTER_PHASE_UNKNOWN } = await loadTypeScriptModule('src/lib/rb/cmdk-cue-preview.svelte.ts');

const TRACK = {
	stable_id: 'trk-owned',
	downbeats: [0, 2, 4, 6, 8, 10, 12, 14],
	stops: [],
	duration_s: 60,
	bpm: 120,
	gridPending: false
};

function harness() {
	const sent = [];
	const replies = [];
	let playing = false;
	const phase = { value: null };
	const dispatch = (command) => {
		sent.push(command.type);
		if (command.type === 'preview_stop') return Promise.resolve(true);
		return new Promise((resolve) => replies.push(resolve));
	};
	const ports = {
		dispatch,
		masterPhase: () => phase.value,
		sounding: () => playing,
		now: () => 0
	};
	const preview = createCuePreview(ports);
	preview.setTrack(TRACK);
	const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
	return {
		preview,
		sent,
		replies,
		settle,
		setPlaying: (value) => {
			playing = value;
		},
		phase
	};
}

describe('a refused jump stops only the source it owns', () => {
	it('sends no stop when the first start was refused and an external preview is started', async () => {
		const { preview, sent, replies, settle } = harness();
		const first = preview.jump(1, false);
		await settle();
		const second = preview.jump(1, false);
		replies.shift()(false);
		await settle();
		preview.stop();
		replies.shift()(false);
		await Promise.all([first, second]);
		assert.deepEqual(sent, ['preview_cue', 'preview_cue']);
	});

	it('sends the deferred stop when the refused jump replaces a source that landed', async () => {
		const { preview, sent, replies, settle, setPlaying } = harness();
		const first = preview.jump(1, false);
		await settle();
		replies.shift()(true);
		setPlaying(true);
		await settle();
		const second = preview.jump(1, false);
		await settle();
		preview.stop();
		replies.shift()(false);
		await Promise.all([first, second]);
		assert.deepEqual(sent, ['preview_cue', 'preview_cue', 'preview_stop']);
	});

	it('a refused later jump keeps the reason the master phase gave', async () => {
		const { preview, sent, replies, settle, setPlaying, phase } = harness();
		const first = preview.jump(1, false);
		await settle();
		replies.shift()(true);
		setPlaying(true);
		await settle();
		phase.value = 'unknown';
		const second = preview.jump(1, false);
		await Promise.all([first, second]);
		assert.equal(preview.state.message, MASTER_PHASE_UNKNOWN);
		assert.equal(preview.state.anchor, 0);
		assert.deepEqual(sent, ['preview_cue']);
	});
});
