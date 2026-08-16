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

test('every inert form control carries BOTH disabled and the exact PARITY-TODO title', () => {
	const offenders = [];
	let checked = 0;
	for (const file of svelteFiles(SRC)) {
		const source = readFileSync(file, 'utf8');
		if (!source.includes(INERT_TITLE)) continue;
		for (const tag of openTags(source)) {
			if (!tag.attrs.includes(INERT_TITLE)) continue;
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
			if (!/PARITY[- ]?TODO/i.test(tag.attrs)) continue;
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
	for (const file of [...svelteFiles(SRC), join(SRC, 'lib/settings/catalog.ts')]) {
		const source = readFileSync(file, 'utf8');
		for (const match of source.matchAll(/\bconst\s+\w+\s*=\s*(['"])([^'"\n]*PARITY[- ]?TODO[^'"\n]*)\1/gi)) {
			declarations.push({ file: file.slice(SRC.length + 1), value: match[2] });
		}
	}
	assert.ok(
		declarations.length >= 4,
		`expected the inert title to be declared in several modules, found ${declarations.length}`
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

test('the settings modules spell the inert title exactly like the components do', () => {
	for (const rel of ['lib/settings/catalog.ts', 'lib/components/settings/SettingsOverlay.svelte']) {
		const source = readFileSync(join(SRC, rel), 'utf8');
		assert.ok(
			source.includes(`'${INERT_TITLE}'`),
			`${rel} must declare the canonical inert title verbatim`
		);
	}
});

// -------------------------------------------------- an unimplemented setting

test('every unimplemented setting shows the PARITY-TODO title', () => {
	const stubs = catalog.SETTINGS_CATALOG.filter((def) => !def.implemented);
	assert.ok(stubs.length > 0, 'the catalog must still carry parity stubs for this rule to mean anything');
	for (const def of stubs) {
		assert.equal(
			def.title,
			INERT_TITLE,
			`setting ${def.id} is a stub but its title is ${JSON.stringify(def.title)}`
		);
	}
});

test('no unimplemented setting is allowlisted, so it structurally cannot be written', () => {
	for (const def of catalog.SETTINGS_CATALOG) {
		if (def.implemented) continue;
		assert.equal(
			apply.isAllowedSettingKey(def.id),
			false,
			`setting ${def.id} is implemented:false but appears in ALLOWED_SETTING_KEYS - ` +
				'a placeholder that can silently persist a value'
		);
		assert.throws(
			() => apply.applySettingChange(def.id, true),
			/unknown or disallowed setting key/,
			`applySettingChange must refuse the unimplemented key ${def.id}`
		);
	}
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

test('every implemented boolean or enum setting really is writable', () => {
	// The mirror of the rule above: a control that looks live must have a
	// mutator behind it, or clicking it throws mid-set.
	for (const def of catalog.SETTINGS_CATALOG) {
		if (!def.implemented) continue;
		if (def.control.kind !== 'boolean' && def.control.kind !== 'enum') continue;
		assert.equal(
			apply.isAllowedSettingKey(def.id),
			true,
			`setting ${def.id} renders live but is not in ALLOWED_SETTING_KEYS - clicking it throws`
		);
	}
});
