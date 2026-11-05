/**
 * Behavior tests for transition-in PUT / toast / refresh order (AGENT-51 / issue #8453).
 *
 * Regression lines:
 * - if PUT succeeds but refreshHotCues never runs then broken
 * - if the success toast is after refresh then a refresh throw can look like the PUT failed
 * - if refresh throws then the runner resolves with a saved-but-no-refresh toast, not a not-set toast
 */

import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/transition-in-menu.ts');
});

const defaultClick = { stableId: 'track-a', timeS: 12.5 };

function makeDeps(overrides = {}) {
	const record = [];
	const putArgs = [];
	const deps = {
		setTransitionInCue: async (stableId, inMs) => {
			record.push('put');
			putArgs.push([stableId, inMs]);
			return { bar: 7 };
		},
		pushToast: (message, kind) => {
			record.push(`toast:${kind}:${message}`);
		},
		deckIds: [1, 2, 3, 4],
		getDeckState: (id) => (id === 1 ? { stable_id: 'track-a' } : { stable_id: 'other' }),
		refreshHotCues: async (id) => {
			record.push(`refresh:${id}`);
		},
		...overrides
	};
	if (overrides.setTransitionInCue) {
		const custom = overrides.setTransitionInCue;
		deps.setTransitionInCue = async (stableId, inMs) => {
			record.push('put');
			putArgs.push([stableId, inMs]);
			return custom(stableId, inMs);
		};
	}
	if (overrides.refreshHotCues) {
		const custom = overrides.refreshHotCues;
		deps.refreshHotCues = async (id) => {
			record.push(`refresh:${id}`);
			return custom(id);
		};
	}
	return { record, putArgs, deps };
}

test('transition-in runner PUTs, success toast, then refreshHotCues in order', async () => {
	const { record, putArgs, deps } = makeDeps();
	await mod.runTransitionInCue(defaultClick, deps);
	assert.deepEqual(record, ['put', 'toast:info:Transition-in cue set at bar 7', 'refresh:1']);
	assert.deepEqual(putArgs, [['track-a', 12500]]);
});

test('refresh reject resolves with saved-but-waveform toast, not not-set', async () => {
	const { record, deps } = makeDeps({
		refreshHotCues: async () => {
			throw new Error('anlz 503');
		}
	});
	await mod.runTransitionInCue(defaultClick, deps);
	assert.deepEqual(record, [
		'put',
		'toast:info:Transition-in cue set at bar 7',
		'refresh:1',
		'toast:error:Transition-in cue saved but waveform did not refresh: anlz 503'
	]);
});

test('PUT throw resolves with not-set toast and no refresh', async () => {
	const { record, deps } = makeDeps({
		setTransitionInCue: async () => {
			throw new Error('NO_BEATGRID');
		}
	});
	await mod.runTransitionInCue(defaultClick, deps);
	assert.ok(!record.some((e) => e.startsWith('refresh:')));
	assert.deepEqual(record, ['put', 'toast:error:Transition-in cue not set: NO_BEATGRID']);
});

test('refreshHotCues only for decks whose stable_id matches click', async () => {
	const record = [];
	const deps = {
		setTransitionInCue: async () => {
			record.push('put');
			return { bar: 7 };
		},
		pushToast: (message, kind) => {
			record.push(`toast:${kind}:${message}`);
		},
		deckIds: [1, 2, 3, 4],
		getDeckState: (id) => {
			if (id === 1 || id === 3) return { stable_id: 'track-a' };
			return { stable_id: 'other' };
		},
		refreshHotCues: async (id) => {
			record.push(`refresh:${id}`);
		}
	};
	await mod.runTransitionInCue(defaultClick, deps);
	const refreshes = record.filter((e) => e.startsWith('refresh:'));
	assert.deepEqual(refreshes, ['refresh:1', 'refresh:3']);
});
