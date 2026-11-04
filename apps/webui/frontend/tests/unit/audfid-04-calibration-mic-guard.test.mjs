// requirement: AUDFID-04 (every Mac app microphone open passes the CUEOUT-8393 guard)
// [if] the cue alignment calibration opens its microphone [then] it calls the same guard as the AUDIO IN Test before getUserMedia ⛔️
// [if] the Mac app's input name unlock opens its microphone while MAIN plays [then] it is refused before getUserMedia and logged ⛔️
// [if] a new getUserMedia call site appears in the player [then] it must be on the guarded list below ⛔️
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';

import { MIC_WHILE_ROOM_PLAYS_REFUSAL, micOpenRefusal } from '../../src/lib/player/io-device-test.ts';

const PLAYER = new URL('../../src/lib/player/', import.meta.url);
const read = (name) => readFileSync(new URL(name, PLAYER), 'utf8');

/** Index of the first statement-level match after `from`, or -1. */
function after(source, from, needle) {
	const start = source.indexOf(from);
	assert.ok(start >= 0, `${from} not found`);
	const at = source.indexOf(needle, start);
	return at;
}

test('if calibration reaches getUserMedia before the mic guard then a calibration during a set can silence the Bluetooth MAIN', () => {
	const source = read('cue-align-audio.ts');
	const gate = after(source, 'async getUserMedia(): Promise<_MicStreamHandle>', 'assertMicOpenAllowedNow();');
	const open = after(source, 'async getUserMedia(): Promise<_MicStreamHandle>', 'mediaDevices.getUserMedia(');
	assert.ok(gate > 0 && open > 0 && gate < open, 'the calibration mic must be refused before it opens');
	assert.match(source, /import \{ assertMicOpenAllowedNow \} from '\$lib\/player\/io-device-test-audio';/);
});

test('if the Mac app input name unlock opens a mic while MAIN plays then the guard has a hole', () => {
	const source = read('headphones.ts');
	const fn = source.slice(source.indexOf('async function _unlockWebviewInputs('));
	const gate = fn.indexOf('micOpenRefusal({');
	const open = fn.indexOf('_unlockHeadphoneOutputLabels(');
	assert.ok(gate > 0 && open > 0 && gate < open, 'the unlock must consult the refusal first');
	assert.match(fn.slice(gate, open), /macAppWebview: true/);
	assert.match(fn.slice(gate, open), /return false;/);
});

test('if the shared guard stops refusing while MAIN plays in the Mac app then calibration is unguarded too', () => {
	assert.equal(micOpenRefusal({ macAppWebview: true, masterRms: 0.02 }), MIC_WHILE_ROOM_PLAYS_REFUSAL);
	assert.equal(micOpenRefusal({ macAppWebview: true, masterRms: 0 }), null);
});

test('if a getUserMedia call site is added outside the guarded set then a mic can open unguarded', () => {
	const guarded = new Map([
		['io-device-test-audio.ts', 1],
		['cue-align-audio.ts', 1],
		['headphones.ts', 1]
	]);
	for (const name of readdirSync(PLAYER).filter((file) => file.endsWith('.ts'))) {
		const calls = (read(name).match(/\.getUserMedia\(\{/g) ?? []).length;
		if (calls === 0) continue;
		assert.equal(calls, guarded.get(name), `${name} opens a microphone ${calls}x; route it through assertMicOpenAllowedNow`);
	}
});
