import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// H13 + M20 - inert controls stay disabled and say so; nothing is ever wired to
// fake data. The literal is hand-repeated across eleven components and the rule
// lives only in CLAUDE.md prose, which is exactly the shape a refactor renames
// in ten files and quietly stubs in the eleventh.
//
// Regression lines:
// - if an inert button/input loses `disabled` then a dead control looks live
// - if the PARITY-TODO wording is reworded anywhere then the rule has split in two
// - if an `implemented: false` setting is added to ALLOWED_SETTING_KEYS then a
//   placeholder can silently persist a pref
// - if applySettingChange stops throwing on an unimplemented key then the AI
//   apply path can write an arbitrary pref

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const INERT_TITLE = 'not implemented - see PARITY-TODO';
const PARITY_TODO_FLEX = 'PARITY-TODO'.replace('-', '[- ]?');
const PARITY_TODO_FLEX_RE = new RegExp(PARITY_TODO_FLEX, 'i');

function parityTodoConstRe() {
	return new RegExp(
		String.raw`\bconst\s+\w+\s*=\s*(['"])([^'"\n]*` + PARITY_TODO_FLEX + String.raw`[^'"\n]*)\1`,
		'gi',
	);
}

/** Elements where `disabled` is a real, enforced HTML attribute. */
const DISABLEABLE = new Set(['button', 'input', 'select', 'textarea', 'fieldset', 'optgroup', 'option']);

function svelteFiles(dir) {
	const out = [];
	for (const entry of readdirSync(dir)) {
		const path = join(dir, entry);
		if (statSync(path).isDirectory()) out.push(...svelteFiles(path));
		else if (entry.endsWith('.svelte')) out.push(path);
	}
	return out;
}

/**
 * Every element open-tag in a Svelte file, brace- and quote-aware so inline
 * handlers like `onclick={() => x}` do not truncate the tag at their arrow.
 */
function openTags(source) {
	const tags = [];
	for (let i = 0; i < source.length; i++) {
		if (source[i] !== '<' || !/[a-zA-Z]/.test(source[i + 1] ?? '')) continue;
		const nameMatch = /^<([a-zA-Z][\w.$-]*)/.exec(source.slice(i, i + 64));
		if (nameMatch === null) continue;
		let depth = 0;
		let quote = null;
		let j = i + nameMatch[0].length;
		for (; j < source.length; j++) {
			const ch = source[j];
			if (quote !== null) {
				if (ch === quote) quote = null;
				continue;
			}
			if (ch === '"' || ch === "'") quote = ch;
			else if (ch === '{') depth++;
			else if (ch === '}') depth--;
			else if (ch === '>' && depth === 0) break;
		}
		tags.push({ name: nameMatch[1].toLowerCase(), attrs: source.slice(i, j + 1) });
		i = j;
	}
	return tags;
}

let catalog;
let apply;

before(async () => {
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	apply = await loadTypeScriptModule('src/lib/settings/apply.ts');
});

// ------------------------------------------------------ the markup contract

/**
 * Names of module-level constants in `source` that hold exactly INERT_TITLE.
 * Inert controls now reference the shared constant (as TrackActions.svelte's
 * `title={INERT_TITLE}` did, a68c83a59 UXR-02) instead of repeating the literal,
 * so a literal-only scan finds none and would pass vacuously.
 */
function inertTitleConstants(source) {
	const names = new Set();
	for (const match of source.matchAll(/\bconst\s+(\w+)\s*=\s*(['"])([^'"\n]*)\2/g)) {
		if (match[3] === INERT_TITLE) names.add(match[1]);
	}
	return names;
}

/** True when an open tag carries the inert title, literally or via a constant. */
function carriesInertTitle(attrs, constantNames) {
	if (attrs.includes(INERT_TITLE)) return true;
	for (const name of constantNames) {
		if (new RegExp(`\\{\\s*${name}\\s*\\}`).test(attrs)) return true;
	}
	return false;
}

test('inert-title detection sees a constant-referenced control, and flags one missing disabled', () => {
	// Mutation control: without constant resolution the first case is invisible.
	const source = `<script>const INERT = '${INERT_TITLE}';</script>\n<button title={INERT}>x</button>`;
	const names = inertTitleConstants(source);
	const button = openTags(source).find((tag) => tag.name === 'button');
	assert.equal(carriesInertTitle(button.attrs, names), true);
	assert.equal(/\bdisabled\b/.test(button.attrs), false, 'fixture must be the offending shape');
});

test('PARITY-TODO flex regex matches hyphen, space, glued, and case variants', () => {
	const hyphen = 'PARITY-TODO';
	const space = hyphen.replace('-', ' ');
	const glued = hyphen.replace('-', '');
	assert.equal(PARITY_TODO_FLEX_RE.test(hyphen), true);
	assert.equal(PARITY_TODO_FLEX_RE.test(space), true);
	assert.equal(PARITY_TODO_FLEX_RE.test(glued), true);
	assert.equal(PARITY_TODO_FLEX_RE.test(hyphen.toLowerCase()), true);
	assert.equal(PARITY_TODO_FLEX_RE.test('unrelated title'), false);

	const source = [
		`const A = '${hyphen}';`,
		`const B = '${space}';`,
		`const C = '${glued}';`,
		`const D = '${hyphen.toLowerCase()}';`,
		`const E = 'unrelated';`,
	].join('\n');
	const values = [...source.matchAll(parityTodoConstRe())].map((m) => m[2]);
	assert.deepEqual(values, [hyphen, space, glued, hyphen.toLowerCase()]);
});

test('every inert form control carries BOTH disabled and the exact PARITY-TODO title', () => {
	const offenders = [];
	let checked = 0;
	for (const file of svelteFiles(SRC)) {
		const source = readFileSync(file, 'utf8');
		if (!source.includes(INERT_TITLE)) continue;
		const constantNames = inertTitleConstants(source);
		for (const tag of openTags(source)) {
			if (!carriesInertTitle(tag.attrs, constantNames)) continue;
			if (!DISABLEABLE.has(tag.name)) continue;
			checked++;
			if (!/\bdisabled\b/.test(tag.attrs)) {
				offenders.push(`${file.slice(SRC.length + 1)}: <${tag.name}> has the PARITY-TODO title but no disabled`);
			}
		}
	}
	assert.ok(
		checked > 0,
		'no inert form control found at all - if every parity stub is now implemented, ' +
			'retire this assertion deliberately rather than letting it pass vacuously'
	);
	assert.deepEqual(offenders, [], offenders.join('\n'));
});

test('an inert control never gets a reworded PARITY-TODO title of its own', () => {
	// Explanatory prose elsewhere (toasts, column headers, placeholder panels)
	// may say more; a DISABLED CONTROL must say exactly this, so the rule cannot
	// be renamed in ten components and quietly restated in the eleventh.
	const offenders = [];
	for (const file of svelteFiles(SRC)) {
		const source = readFileSync(file, 'utf8');
		for (const tag of openTags(source)) {
			if (!DISABLEABLE.has(tag.name)) continue;
			if (!PARITY_TODO_FLEX_RE.test(tag.attrs)) continue;
			if (tag.attrs.includes(INERT_TITLE)) continue;
			// A constant reference such as title={INERT} resolves elsewhere; the
			// declaration check below pins those.
			offenders.push(`${file.slice(SRC.length + 1)}: <${tag.name}> ${tag.attrs.replace(/\s+/g, ' ').slice(0, 120)}`);
		}
	}
	assert.deepEqual(offenders, [], `inert controls with drifted wording:\n${offenders.join('\n')}`);
});

test('every module-level PARITY-TODO constant holds the identical string', () => {
	const declarations = [];
	for (const file of svelteFiles(SRC)) {
		const source = readFileSync(file, 'utf8');
		for (const match of source.matchAll(parityTodoConstRe())) {
			declarations.push({ file: file.slice(SRC.length + 1), value: match[2] });
		}
	}
	// Positive control: the scan must find the declaration SettingsOverlay is
	// pinned to keep (next test). A count floor rotted as controls went live:
	// LIBWS-04 made the preview bar's Play real, leaving two declarations.
	assert.ok(
		declarations.some((d) => d.file.endsWith('settings/SettingsOverlay.svelte')),
		`the scan found no inert-title constant in SettingsOverlay.svelte, so it is not measuring (found ${declarations.length})`
	);
	const drifted = declarations.filter((d) => d.value !== INERT_TITLE);
	assert.deepEqual(
		drifted,
		[],
		`these shared inert-title constants drifted:\n${drifted
			.map((d) => `  ${d.file}: ${JSON.stringify(d.value)}`)
			.join('\n')}`
	);
});

test('the settings overlay spells the inert title exactly like the components do', () => {
	// SettingsOverlay keeps its inert-row render path for any future placeholder;
	// catalog.ts no longer declares the title because it ships no stub (SETSTUB-01).
	const rel = 'lib/components/settings/SettingsOverlay.svelte';
	const source = readFileSync(join(SRC, rel), 'utf8');
	assert.ok(source.includes(`'${INERT_TITLE}'`), `${rel} must declare the canonical inert title verbatim`);
});

// ------------------------------------------------- SETSTUB-01: no stub rows

test('SETSTUB-01 if a settings row ships implemented:false then a placeholder is back in the catalog', () => {
	const stubs = catalog.SETTINGS_CATALOG.filter((def) => def.implemented !== true);
	assert.deepEqual(
		stubs.map((def) => def.id),
		[],
		'the settings catalog ships only working rows; build the setting or leave it out'
	);
	// Positive control: the catalog was really read, and the one stub that was
	// built (SETSTUB-02) is present as a real row.
	assert.ok(catalog.SETTINGS_CATALOG.length > 30, `control: catalog rows read (${catalog.SETTINGS_CATALOG.length})`);
	const quantize = catalog.SETTINGS_CATALOG.find((def) => def.id === 'deck_default_quantize');
	assert.ok(quantize, 'control: the built Quantize-on-new-decks row is in the catalog');
	assert.equal(quantize.implemented, true);
	assert.notEqual(quantize.title, INERT_TITLE);
});

test('SETSTUB-01 if a "(todo)" parity group returns then the empty Rekordbox / djay sections are back', () => {
	const ids = catalog.SETTING_GROUPS.map((g) => g.id);
	assert.ok(ids.includes('performance'), 'control: the group list was read');
	assert.ok(!ids.includes('rekordbox'), 'the Rekordbox (todo) group must stay removed');
	assert.ok(!ids.includes('djay'), 'the djay Pro (todo) group must stay removed');
	assert.deepEqual(catalog.SETTING_GROUPS.filter((g) => /\(todo\)/i.test(g.label)), []);
});

test('the removed parity-stub ids stay structurally unwritable', () => {
	// All 32 former stub ids: 30 removed, rb.quantize rebuilt under the new id
	// deck_default_quantize, and rb.master_tempo removed until the engine has a
	// seam for it. None may creep into the allowlist under its old id.
	const RETIRED = [
		'rb.quantize', 'rb.master_tempo', 'rb.sync_mode', 'rb.vinyl_mode', 'rb.jog_sensitivity',
		'rb.hot_cue_colors', 'rb.pad_mode', 'rb.waveform_zoom', 'rb.grid_edit', 'rb.auto_gain',
		'rb.karaoke', 'rb.export_usb', 'rb.analysis_quality', 'rb.phrase_analysis', 'rb.mytag_layout',
		'rb.track_info_fields', 'rb.keyboard_map', 'rb.dual_deck_layout', 'djay.automix', 'djay.eq_kill',
		'djay.effects_rack', 'djay.stems_ui', 'djay.library_source', 'djay.streaming', 'djay.cue_points',
		'djay.midi_learn', 'djay.audio_device', 'djay.sample_rate', 'djay.buffer_size', 'djay.recording',
		'djay.video', 'djay.neumann'
	];
	assert.equal(RETIRED.length, 32, 'control: every former stub id is listed');
	for (const id of RETIRED) {
		assert.equal(apply.isAllowedSettingKey(id), false, `${id} must not be allowlisted`);
		assert.throws(
			() => apply.applySettingChange(id, true),
			/unknown or disallowed setting key/,
			`applySettingChange must refuse the retired key ${id}`
		);
	}
	// Opposite direction: the built replacement IS writable, so this refusal
	// is not just an allowlist that refuses everything.
	assert.equal(apply.isAllowedSettingKey('deck_default_quantize'), true);
});

test('the AI apply path refuses any key outside the allowlist', () => {
	for (const key of ['', 'theme.evil', '__proto__', 'rekordbox_sync_everything', 'constructor']) {
		assert.equal(apply.isAllowedSettingKey(key), false);
		assert.throws(
			() => apply.applySettingChange(key, true),
			/unknown or disallowed setting key/,
			`arbitrary key ${JSON.stringify(key)} must be refused`
		);
	}
});

test('every implemented boolean, enum, multi_bool or number setting really is writable', () => {
	// The mirror of the rule above: a control that looks live must have a
	// mutator behind it, or clicking it throws mid-set. The per-key ids of a
	// multi_bool row are the `auto_sync.<key>` allowlist entries, so they are
	// expanded rather than asserted against the row id.
	const LIVE_KINDS = new Set(['boolean', 'enum', 'multi_bool', 'number']);
	for (const def of catalog.SETTINGS_CATALOG) {
		if (!def.implemented) continue;
		if (!LIVE_KINDS.has(def.control.kind)) continue;
		const keys =
			def.control.kind === 'multi_bool'
				? def.control.keys.map((key) => `${def.id}.${key.id}`)
				: [def.id];
		for (const key of keys) {
			assert.equal(
				apply.isAllowedSettingKey(key),
				true,
				`setting ${key} renders live but is not in ALLOWED_SETTING_KEYS - clicking it throws`
			);
		}
	}
});
