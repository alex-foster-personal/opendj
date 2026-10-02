// Guards the .svelte compiler override in knip.js. See that file's header for the
// knip 6.32.2 bug it works around. Without the override, knip's own scraper matches
// the letters "import" inside identifiers such as importPct / importRunning, emits
// broken JavaScript, and loses every specifier after the first corrupted match.
//
// These assert the PRESENCE of the recovered specifiers rather than the absence of
// corruption: a compiler that returned nothing at all would satisfy "no corruption"
// while telling knip that the whole app is dead.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { svelteImports } from '../../knip.js';

const read = (relative) =>
	readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');

test('a dynamic import survives an identifier that merely starts with "import"', () => {
	const source = [
		'<script lang="ts">',
		"	import { thing } from 'dep-static';",
		'	const importRunning = thing;',
		'	async function go() {',
		"		const { open } = await import('dep-dyn');",
		'		open(importRunning);',
		'	}',
		'</script>',
		'<button onclick={go}>go</button>'
	].join('\n');
	const compiled = svelteImports(source);
	assert.ok(
		compiled.includes("from 'dep-static'"),
		'if the static specifier is dropped then the knip svelte compiler is broken'
	);
	assert.ok(
		compiled.includes("import('dep-dyn')"),
		'if a dynamic import after an import-prefixed identifier is dropped then knip will report its package unused - broken'
	);
});

test('the real SetupOverlay folder picker import reaches knip', () => {
	// The picker's shell packages are imported by src/lib/shell/native-shell.ts,
	// which knip reads natively; the overlay reaches them through that module.
	const compiled = svelteImports(read('src/lib/components/setup/SetupOverlay.svelte'));
	assert.ok(
		compiled.includes("from '$lib/shell/native-shell'"),
		'if the native folder picker import is dropped then frontend.unused_deps regresses - broken'
	);
	assert.ok(
		read('src/lib/shell/native-shell.ts').includes("import('@tauri-apps/plugin-dialog')"),
		'if the bridge stops importing the dialog plugin then knip reports it unused - broken'
	);
});

test('every .svelte import extraction is syntactically valid JavaScript', async () => {
	const { default: ts } = await import('typescript');
	const { globSync } = await import('node:fs');
	const files = globSync('src/**/*.svelte', {
		cwd: fileURLToPath(new URL('../../', import.meta.url))
	});
	assert.ok(files.length > 100, 'if the glob finds almost no components then this test measures nothing');
	const broken = [];
	for (const relative of files) {
		const compiled = svelteImports(read(relative));
		if (!compiled) continue;
		const parsed = ts.createSourceFile(
			'extracted.ts',
			compiled,
			ts.ScriptTarget.ESNext,
			true,
			ts.ScriptKind.TS
		);
		if (parsed.parseDiagnostics.length > 0) broken.push(relative);
	}
	assert.deepEqual(
		broken,
		[],
		'if any component extraction fails to parse then knip stops reading it there and silently under-reports - broken'
	);
});

test('a template-only dynamic import is still seen', () => {
	const compiled = svelteImports(
		'<script>const a = 1;</script>\n{#await import(\'dep-lazy\') then m}{m.x}{/await}'
	);
	assert.ok(
		compiled.includes("import('dep-lazy')"),
		'if template dynamic imports are dropped then the override is narrower than the compiler it replaced - broken'
	);
});
