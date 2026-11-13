import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

// Issue #13868: the skin id list is hand-written in four places. One test reads all four
// and names the missing id and the file, so CLI and Settings reach every skin.

const root = (p) => fileURLToPath(new URL(`../../${p}`, import.meta.url));
const UI_SKIN_TS = root('src/lib/rb/ui-skin.ts');
const SKIN_LAYOUT_TS = root('src/lib/rb/skin-layout.ts');
const CATALOG_PY = fileURLToPath(new URL('../../../../opendj_cli/catalog.py', import.meta.url));

const strings = (body) => [...body.matchAll(/'([^']+)'|"([^"]+)"/g)].map((m) => m[1] ?? m[2]);

function arrayIds(src, name) {
	const m = src.match(new RegExp(`${name}[^=]*=\\s*\\[([\\s\\S]*?)\\]`));
	assert.ok(m, `${name} not found`);
	return strings(m[1].replace(/\/\/.*$/gm, ''));
}

function tupleIds(src, name) {
	const m = src.match(new RegExp(`^${name}[^=]*=\\s*\\(([\\s\\S]*?)\\)`, 'm'));
	assert.ok(m, `${name} not found`);
	return strings(m[1].replace(/#.*$/gm, ''));
}

function recordKeys(src, name) {
	const m = src.match(new RegExp(`${name}[^=]*=\\s*\\{([\\s\\S]*?)\\n\\};`));
	assert.ok(m, `${name} not found`);
	const body = m[1].replace(/\/\/.*$/gm, '');
	return [...body.matchAll(/^\s*(?:'([^']+)'|([A-Za-z_][\w-]*))\s*:/gm)].map((k) => k[1] ?? k[2]);
}

export function parityProblems({ choices, labels, layout, values }) {
	const out = [];
	for (const id of choices) {
		if (!values.includes(id))
			out.push(`${id} missing from UI_SKIN_VALUES in apps/opendj_cli/catalog.py`);
		if (!labels.includes(id)) out.push(`${id} missing from UI_SKIN_LABELS in ui-skin.ts`);
		if (!layout.includes(id)) out.push(`${id} missing from SKIN_LAYOUT in skin-layout.ts`);
	}
	if (out.length === 0 && choices.join(',') !== values.join(',')) {
		out.push(
			`order differs: UI_SKIN_CHOICES [${choices.join(', ')}] vs UI_SKIN_VALUES [${values.join(', ')}]`
		);
	}
	return out;
}

function readAll() {
	const ui = readFileSync(UI_SKIN_TS, 'utf8');
	return {
		choices: arrayIds(ui, 'UI_SKIN_CHOICES'),
		labels: recordKeys(ui, 'UI_SKIN_LABELS'),
		layout: recordKeys(readFileSync(SKIN_LAYOUT_TS, 'utf8'), 'SKIN_LAYOUT'),
		values: tupleIds(readFileSync(CATALOG_PY, 'utf8'), 'UI_SKIN_VALUES')
	};
}

test('every skin id list matches UI_SKIN_CHOICES (ids and order)', () => {
	const lists = readAll();
	assert.ok(lists.choices.length >= 5, `parsed ${lists.choices.length} choices`);
	assert.deepEqual(parityProblems(lists), []);
});

const base = ['default', 'mono-dev', 'light'];

test('an id missing from catalog.py UI_SKIN_VALUES fails naming id and catalog.py', () => {
	const p = parityProblems({
		choices: base,
		labels: base,
		layout: base,
		values: ['default', 'light']
	});
	assert.ok(
		p.some((s) => s.includes('mono-dev') && s.includes('catalog.py')),
		p.join('\n')
	);
});

test('a key missing from UI_SKIN_LABELS or SKIN_LAYOUT fails naming id and table', () => {
	const p = parityProblems({
		choices: base,
		labels: ['default', 'light'],
		layout: ['default', 'mono-dev'],
		values: base
	});
	assert.ok(
		p.some((s) => s.includes('mono-dev') && s.includes('UI_SKIN_LABELS')),
		p.join('\n')
	);
	assert.ok(
		p.some((s) => s.includes('light') && s.includes('SKIN_LAYOUT')),
		p.join('\n')
	);
});

test('a different order between UI_SKIN_CHOICES and UI_SKIN_VALUES fails showing both', () => {
	const p = parityProblems({
		choices: base,
		labels: base,
		layout: base,
		values: ['light', 'default', 'mono-dev']
	});
	assert.equal(p.length, 1);
	assert.match(p[0], /\[default, mono-dev, light\].*\[light, default, mono-dev\]/);
});
