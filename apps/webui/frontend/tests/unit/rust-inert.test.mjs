/**
 * Rust engine mode grays out what it cannot play (NAE-15). The DOM half is
 * browser-checked; this pins the map from control names to commands.
 *
 * Regression lines:
 * - if a mapped control name is not a real `data-performance-control` in a
 *   component then broken: the map would gray out nothing and look fine
 * - if a mapped command is one Rust mode plays then broken: a working control
 *   would be grayed out
 * - if a `data-rust-command` opt-in names a command Rust mode cannot refuse
 *   then broken for the same reason
 * - if a stem chip does not map to the stem command then broken
 */
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
let sources;

function svelteFiles(dir) {
	return readdirSync(dir).flatMap((name) => {
		const p = join(dir, name);
		if (statSync(p).isDirectory()) return svelteFiles(p);
		return name.endsWith('.svelte') ? [p] : [];
	});
}

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/rust-inert-entry.ts');
	sources = svelteFiles('src/lib/components')
		.map((p) => readFileSync(p, 'utf8'))
		.join('\n');
});

test('every mapped control name exists on a component', () => {
	const names = Object.keys(m.CONTROL_COMMANDS);
	assert.ok(names.length >= 10, 'the map is populated');
	const missing = names.filter((n) => !sources.includes(`data-performance-control="${n}"`));
	assert.deepEqual(missing, []);
	assert.ok(sources.includes('data-performance-control={`stem-${stem.id}`}'), 'stem chips are named');
});

test('every mapped command is one Rust mode can refuse, never one it plays', () => {
	const refusable = new Set([...m.FORWARDED, ...m.WEB_AUDIO_ONLY]);
	const opted = [...sources.matchAll(/data-rust-command="([a-z_]+)"/g)].map((x) => x[1]);
	assert.ok(opted.includes('channel_cue') && opted.includes('auto_play_next_arm'));
	for (const cmd of [...Object.values(m.CONTROL_COMMANDS), ...opted, 'stem_mute']) {
		assert.ok(refusable.has(cmd), `${cmd} is forwarded or Web Audio only`);
	}
	for (const played of ['play', 'cue', 'beat_sync', 'quantize', 'master', 'hot_cue_trigger', 'fader']) {
		assert.equal(Object.values(m.CONTROL_COMMANDS).includes(played), false, played);
	}
});

test('control names resolve to their command; an explicit opt-in wins', () => {
	assert.equal(m.commandForControl('stem-vocals'), 'stem_mute');
	assert.equal(m.commandForControl('key-nudge-up'), 'key_nudge');
	assert.equal(m.commandForControl('play'), null, 'a control Rust mode plays is not mapped');
	assert.equal(m.commandForControl(undefined, 'channel_cue'), 'channel_cue');
	assert.equal(m.commandForControl('play', 'channel_cue'), 'channel_cue');
	assert.equal(m.commandForControl(undefined), null);
});
