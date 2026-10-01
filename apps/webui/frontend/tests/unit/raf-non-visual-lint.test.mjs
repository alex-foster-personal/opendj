// requirement: AUDIOLIVE-11
//
// Lint-style guard: requestAnimationFrame is a PAINT clock. A browser stops
// firing it for a hidden, minimized or occluded page, so any state another
// module reads must not be advanced by it alone.
//
// The rule, in two halves:
// - a `.svelte` component is a render module and may use the frame clock;
// - a `.ts` / `.js` module may reference requestAnimationFrame ONLY if it is in
//   the allowlist below, with a reason, and only as many times as recorded. A
//   new reference - in a new module or in an allowlisted one - fails here and
//   has to be justified in this file.
// A frame loop that publishes state other logic reads must also be paired with
// `createFrameBackstop`, which runs the loop from a timer when frames stall.
//
// This reads the PARSED source (TypeScript AST), not text, so a mention in a
// comment or a string is not a reference and a real call cannot hide behind
// formatting. The parser is validated below against a planted violation.
//
// [if] a non-visual module calls requestAnimationFrame [then] this fails and
//   names the file [⛔️ if hidden-tab state can freeze again unnoticed]
// [if] an allowlisted module gains another reference [then] this fails
// [if] an allowlist entry no longer references the frame clock [then] this
//   fails, so the list cannot rot into a list of exemptions for nothing
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import ts from 'typescript';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const SRC = join(FRONTEND_ROOT, 'src');
const FRAME_CLOCK = 'requestAnimationFrame';
const BACKSTOP = 'createFrameBackstop';

/**
 * Every non-component module allowed to reference the frame clock.
 * `references` is the exact count, so growth is a decision made here.
 * `backstop: true` means the loop publishes state that other logic reads, and
 * the module must therefore also call createFrameBackstop.
 */
const ALLOWLIST = {
	'src/lib/rb/frame-backstop.ts': {
		references: 1,
		why: 'frameOrTimeout: the one sanctioned frame wait, raced against a timer'
	},
	'src/lib/rb/audio-engine.svelte.ts': {
		references: 2,
		backstop: true,
		why: 'presentation loop: paints the playhead AND publishes audible/position, so it is backstopped'
	},
	'src/lib/audio-engine/rust-engine.ts': {
		references: 3,
		backstop: true,
		why: 'position mirror for the native engine, read by AutoPlay, so it is backstopped'
	},
	'src/lib/ui/clamp-to-viewport.ts': { references: 2, why: 'popover placement after layout: paint only' },
	'src/lib/rb/vibe.svelte.ts': { references: 3, why: 'pointer-activity meter decay: a display value, driven by pointer moves a hidden page never gets' },
	'src/lib/rb/mixer-performance-listeners.ts': { references: 2, why: 'fader ghost guidance: when to SHOW a hint, paint only' },
	'src/lib/player/headphones.ts': {
		references: 4,
		why: 'signal meters: sampled for display. KNOWN GAP: the IPC snapshot also carries them, stale while hidden but stamped with measured_at'
	},
	'src/lib/rb/performance-ipc.svelte.ts': { references: 1, why: 'preset settle poll: each frame wait is raced against a timer deadline, so it is bounded' },
	'src/lib/rb/performance-rescue-restore.svelte.ts': {
		references: 4,
		why: 'rescue decode poll: bounded by its ceiling timer, which re-reads the state itself'
	}
};

//-----------------------------------------------------------------------------
// source walk
//-----------------------------------------------------------------------------

function modulesUnder(dir) {
	const found = [];
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const path = join(dir, entry.name);
		if (entry.isDirectory()) found.push(...modulesUnder(path));
		else if (/\.(ts|js|mjs)$/.test(entry.name) && !/\.(test|spec|stories)\.|\.d\.ts$/.test(entry.name)) found.push(path);
	}
	return found;
}

/** Count identifier references to `name` in real code (never comments or strings). */
function countIdentifier(sourceText, name, fileName = 'module.ts') {
	const source = ts.createSourceFile(fileName, sourceText, ts.ScriptTarget.Latest, false);
	let count = 0;
	const visit = (node) => {
		if (ts.isIdentifier(node) && node.text === name) count += 1;
		ts.forEachChild(node, visit);
	};
	visit(source);
	return count;
}

/** Count direct CALLS of `name`. An import of it is not a use of it. */
function countCalls(sourceText, name, fileName = 'module.ts') {
	const source = ts.createSourceFile(fileName, sourceText, ts.ScriptTarget.Latest, false);
	let count = 0;
	const visit = (node) => {
		if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === name) count += 1;
		ts.forEachChild(node, visit);
	};
	visit(source);
	return count;
}

function scan() {
	const files = modulesUnder(SRC);
	const references = new Map();
	const backstops = new Map();
	for (const file of files) {
		const text = readFileSync(file, 'utf8');
		if (!text.includes(FRAME_CLOCK) && !text.includes(BACKSTOP)) continue;
		const key = relative(FRONTEND_ROOT, file).split('\\').join('/');
		const frames = countIdentifier(text, FRAME_CLOCK, file);
		if (frames > 0) references.set(key, frames);
		backstops.set(key, countCalls(text, BACKSTOP, file));
	}
	return { fileCount: files.length, references, backstops };
}

//-----------------------------------------------------------------------------
// the instrument first: it must be able to see a violation, and to not see one
//-----------------------------------------------------------------------------

test('AUDIOLIVE-11 lint: the reference counter sees real calls and ignores comments and strings', () => {
	const planted = [
		'// requestAnimationFrame in a comment is not a reference',
		'/* nor is requestAnimationFrame(tick) in a block comment */',
		"const label = 'requestAnimationFrame';",
		'export function loop(): void {',
		'	requestAnimationFrame(loop);',
		'	const raf = globalThis.requestAnimationFrame;',
		'	void raf;',
		'}'
	].join('\n');
	assert.equal(countIdentifier(planted, FRAME_CLOCK), 2, 'if the counter does not find exactly the two real references then broken - every verdict below is unmeasured');
	assert.equal(countIdentifier('export const quiet = 1;', FRAME_CLOCK), 0, 'control: a module with no reference reports none');
	const imported = "import { createFrameBackstop } from './frame-backstop';\nexport const unused = 1;";
	assert.equal(countCalls(imported, BACKSTOP), 0, 'if an import alone counts as a call then broken - a module could import the backstop, never arm it, and pass');
	assert.equal(countCalls(`${imported}\ncreateFrameBackstop(() => null, () => {}).arm();`, BACKSTOP), 1, 'control: a real call is counted');
});

test('AUDIOLIVE-11 lint: the walk covers the source tree and finds the known frame loops', () => {
	const { fileCount, references } = scan();
	assert.ok(fileCount > 300, `if the walk sees only ${fileCount} modules then it is pointed at the wrong directory and proves nothing`);
	assert.ok(
		(references.get('src/lib/rb/audio-engine.svelte.ts') ?? 0) > 0,
		'if the engine frame loop is not found then the scan is blind - an empty result would read as a clean tree'
	);
});

//-----------------------------------------------------------------------------
// the rule
//-----------------------------------------------------------------------------

test('AUDIOLIVE-11 lint: no non-visual module references requestAnimationFrame outside the allowlist', () => {
	const { references } = scan();
	const offenders = [...references.keys()].filter((file) => ALLOWLIST[file] === undefined);
	assert.deepEqual(
		offenders,
		[],
		'if a non-component module uses the frame clock then broken - a hidden tab stops firing it, so whatever it advances ' +
			'freezes. Drive state from the AudioContext clock, an audio event or a timer (see src/lib/rb/frame-backstop.ts); ' +
			'if this really is paint-only, add it to ALLOWLIST with the reason'
	);
});

test('AUDIOLIVE-11 lint: an allowlisted module holds exactly its recorded number of references', () => {
	const { references } = scan();
	const drifted = Object.entries(ALLOWLIST)
		.filter(([file, entry]) => (references.get(file) ?? 0) !== entry.references)
		.map(([file, entry]) => `${file}: recorded ${entry.references}, found ${references.get(file) ?? 0} (${entry.why})`);
	assert.deepEqual(
		drifted,
		[],
		'if an allowlisted module references the frame clock a different number of times than recorded then broken - ' +
			'a new use must be classified here, and a removed one must shrink or drop the entry'
	);
});

test('AUDIOLIVE-11 lint: a frame loop that publishes state is paired with the frame backstop', () => {
	const { backstops } = scan();
	for (const [file, entry] of Object.entries(ALLOWLIST)) {
		if (entry.backstop !== true) continue;
		assert.ok(
			(backstops.get(file) ?? 0) > 0,
			`if ${file} runs its state-publishing frame loop without createFrameBackstop then broken - ` +
				'its state freezes the moment the page is hidden'
		);
	}
});
