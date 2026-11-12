// requirement: NATIVE-03
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const IPC_SRC = readFileSync(`${SRC}/lib/rb/performance-ipc.svelte.ts`, 'utf8');

function sliceAfter(haystack, needle, maxLen = 800) {
	const start = haystack.indexOf(needle);
	assert.notEqual(start, -1, `missing ${needle}`);
	return haystack.slice(start, start + maxLen);
}

test('hot_cue_save gates BeatSyncMax snap on hasTrustedBeatGrid', () => {
	assert.match(
		IPC_SRC,
		/import\s*\{[^}]*hasTrustedBeatGrid[^}]*\}\s*from\s*'\$lib\/player\/grid-features'/
	);
	const saveArm = sliceAfter(IPC_SRC, "} else if (command.type === 'hot_cue_save')");
	assert.match(saveArm, /hasTrustedBeatGrid\(/);
	assert.match(saveArm, /hasPhaseCapableBeatGrid\(/);
	assert.match(saveArm, /quantizeToNearestDownbeat\(/);
});

test('hot_cue_trigger gates planHotCueTrigger on hasTrustedBeatGrid', () => {
	// The whole arm, up to the next command branch: PR #12158 put the SETREC-07
	// capture call ahead of the plan, so a fixed-length window no longer reached it.
	const triggerArm = sliceAfter(
		IPC_SRC,
		"} else if (command.type === 'hot_cue_trigger')",
		4000
	).split(/\n\t\} else if \(command\.type === /)[0];
	assert.ok(triggerArm, 'hot_cue_trigger arm');
	assert.match(triggerArm, /planHotCueTrigger\(/);
	assert.match(triggerArm, /hasTrustedBeatGrid\(/);
	assert.match(triggerArm, /hasPhaseCapableBeatGrid\(/);
});
