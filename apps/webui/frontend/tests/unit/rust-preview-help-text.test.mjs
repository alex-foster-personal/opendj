// Settings > Performance > Audio engine help text matches what rust mode refuses. -Claude
//
// [if] the help text says a feature refuses in the Rust engine preview [then] every command
//   behind that feature really is refused [else stop: the text promises a refusal that does
//   not happen, or a DJ avoids a feature that works].
// [if] rust mode refuses a command [then] the help text names its feature, or the command is
//   pinned below as one the text deliberately leaves out [else stop: the DJ finds out on press].
//
// "Refused" means one of the two lists rust mode actually refuses by:
//   - WEB_AUDIO_ONLY in rust-mode.svelte.ts (the page refuses by name), or
//   - FORWARDED in rust-mode.svelte.ts AND the engine's LATER list in protocol.rs (the engine
//     answers not_implemented and lists it in its hello, so the page greys it out).
// Reads the source files as text: rust-mode.svelte.ts carries Svelte runes that node:test
// cannot load, and protocol.rs is Rust.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const SRC = new URL('../../src/lib/', import.meta.url);
const setting = readFileSync(new URL('settings/audio-engine-setting.ts', SRC), 'utf8');
const rustMode = readFileSync(new URL('audio-engine/rust-mode.svelte.ts', SRC), 'utf8');
const protocol = readFileSync(new URL('../../../../audio-engine/src/protocol.rs', import.meta.url), 'utf8');

/** Each feature the help text may claim refuses, and the commands that make it refuse. */
const CLAIMABLE = {
	'key sync': ['key_sync'],
	slip: ['slip'],
	stems: ['stem_load', 'stem_separate', 'stem_mute', 'stem_solo', 'stem_gain', 'stem_eq_mode'],
	'safety loops': ['safety_loop_save', 'safety_loop_arm', 'safety_loop_clear'],
	'autoplay next': ['auto_play_next_arm', 'auto_play_next_cancel', 'auto_play_decide_now'],
	'the headphone cue bus': ['channel_cue', 'headphone_mix', 'headphone_level', 'head_delay_ms'],
	'grid edits': ['grid_edit'],
	'color fx': ['color_fx'],
	'headphone devices': [
		'headphone_outputs_refresh',
		'headphone_output_acquire',
		'headphone_input_select',
		'headphone_calibrate',
		'headphone_calibrate_abort',
		'headphone_alignment_mode',
		'output_mode',
		'headphone_output_select',
		'headphone_master_select'
	]
};

/** Refused commands the help text deliberately does not name: internal, or not a control a DJ
 * reaches from the deck (each still refuses by name on press). */
const UNNAMED = new Set([
	'loop_interval_mode',
	'loop_interval_base',
	'master_delay_ms',
	'rescue_resume',
	'rescue_stop_all',
	// DECKUX-6794: behind a default-off flag, so the preview's help text has no feature to name yet.
	'grid_jump',
	// DECKMOVE-01: an artwork right-click action, refused by name with its own error; the native port is a follow-up.
	'deck_move'
]);

function detail() {
	const m = setting.match(/detail:\s*\n?\s*'([^']+)'/);
	assert.ok(m, 'audio-engine-setting.ts has a single-quoted detail string');
	return m[1];
}

/** The features the text says refuse, from its one "In the preview, ... refuse by name" sentence. */
function claimedFeatures() {
	const m = detail().match(/In the preview, ([^;]+) refuse by name/);
	assert.ok(m, 'the help text has one "In the preview, ... refuse by name" clause');
	return m[1]
		.toLowerCase()
		.split(/, and |, | and /)
		.map((s) => s.trim())
		.filter(Boolean);
}

function setMembers(name) {
	const m = rustMode.match(new RegExp(`export const ${name}: ReadonlySet<string> = new Set\\(\\[([^\\]]+)\\]`));
	assert.ok(m, `rust-mode.svelte.ts declares ${name}`);
	return new Set([...m[1].matchAll(/'([a-z_]+)'/g)].map((x) => x[1]));
}

function engineLater() {
	const m = protocol.match(/const LATER: &\[\(&str, &str\)\] = &\[([\s\S]*?)\n\];/);
	assert.ok(m, 'protocol.rs declares the LATER list');
	return new Set([...m[1].matchAll(/\(\s*"([a-z_]+)",/g)].map((x) => x[1]));
}

const webAudioOnly = setMembers('WEB_AUDIO_ONLY');
const forwarded = setMembers('FORWARDED');
const later = engineLater();
const refused = (command) => webAudioOnly.has(command) || (forwarded.has(command) && later.has(command));
const allRefused = [...webAudioOnly, ...[...forwarded].filter((c) => later.has(c))];

test('every refusal the help text claims is a real refusal in rust mode', () => {
	const claimed = claimedFeatures();
	assert.ok(claimed.length > 0, 'the help text claims at least one refusal');
	for (const feature of claimed) {
		const commands = CLAIMABLE[feature];
		assert.ok(commands, `the help text claims "${feature}" refuses, but this test has no command list for it; pin one in CLAIMABLE`);
		for (const command of commands) {
			assert.ok(refused(command), `the help text says "${feature}" refuses, but rust mode does not refuse ${command} (not in WEB_AUDIO_ONLY, nor in FORWARDED and the engine's LATER list)`);
		}
	}
});

test('every command rust mode refuses is named in the help text or pinned as unnamed', () => {
	const claimed = new Set(claimedFeatures());
	for (const command of allRefused) {
		const feature = Object.keys(CLAIMABLE).find((f) => CLAIMABLE[f].includes(command));
		if (feature === undefined) {
			assert.ok(UNNAMED.has(command), `rust mode refuses ${command}, but no help-text feature covers it and it is not pinned in UNNAMED`);
		} else {
			assert.ok(claimed.has(feature), `rust mode refuses ${command}, so the help text must name "${feature}"; it claims: ${[...claimed].join(', ')}`);
		}
	}
	for (const command of UNNAMED) assert.ok(refused(command), `${command} is pinned as an unnamed refusal but rust mode no longer refuses it; drop it from UNNAMED`);
});

test('the help text does not claim that page-decided features refuse in the preview', () => {
	const decided = setMembers('PAGE_DECIDED');
	assert.ok(decided.has('beat_sync') && decided.has('play'), 'beat sync and play are page-decided in rust mode');
	const clause = claimedFeatures().join(', ');
	assert.doesNotMatch(clause.replace(/key sync/g, ''), /\bsync\b/, `Beat Sync works in the preview, but the help text says it refuses: "${clause}"`);
	assert.doesNotMatch(clause, /hot-cue save|hot cues/, `hot cues run through rustHotCueDriver, but the help text says they refuse: "${clause}"`);
	assert.doesNotMatch(clause.replace(/autoplay next/g, ''), /auto-?play/, `AutoPlay itself is not refused, only AutoPlay Next is: "${clause}"`);
});
