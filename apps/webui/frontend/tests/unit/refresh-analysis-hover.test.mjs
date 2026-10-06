/**
 * requirement: CHROME-09, CHROME-10
 */
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { compile } from 'svelte/compiler';
import { loadTypeScriptModule } from './load-typescript.mjs';

const refresh = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/RefreshAnalysisButton.svelte', import.meta.url)),
	'utf8'
);
// The popover markup is its own module, fetched by the first open (issue
// #3886, /performance bundle budget); the button keeps the state and polling.
const popoverSource = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/RefreshAnalysisPopover.svelte', import.meta.url)),
	'utf8'
);

test('refresh analysis button does not set native title alongside popover hover', () => {
	const buttonStart = refresh.indexOf('data-testid="refresh-analysis"');
	const buttonChunk = refresh.slice(refresh.lastIndexOf('<button', buttonStart), buttonStart + 200);
	assert.doesNotMatch(buttonChunk, /\btitle=/);
	assert.match(popoverSource, /data-testid="refresh-analysis-pop"/);
});

test('refresh click surfaces WIP or running feedback in the popover', () => {
	assert.match(refresh, /clickFeedback/);
	assert.match(refresh, /WIP - not working/);
	assert.match(popoverSource, /data-testid="refresh-click-feedback"/);
});

// Codex P2 on PR #3896: the click result rendered only when fetchError was
// null AND the polled status was idle, so a failed click after a finished run
// (or after a hover fetch error) showed stale state instead of its outcome.
const popover = popoverSource.slice(popoverSource.indexOf('data-testid="refresh-analysis-pop"'));

test('the latest click result renders ahead of the hover error and the polled status', () => {
	const feedbackAt = popover.indexOf('{#if clickFeedback !== null}');
	const errorAt = popover.indexOf('{#if fetchError !== null}');
	const statusAt = popover.indexOf("{#if status !== null && status.phase !== 'idle'}");
	assert.ok(feedbackAt >= 0, 'click feedback must be its own top-level block');
	assert.ok(feedbackAt < errorAt && feedbackAt < statusAt, 'it must render before the stale fields');
	const block = popover.slice(feedbackAt, popover.indexOf('{/if}', feedbackAt));
	assert.match(block, /data-testid="refresh-click-feedback">\{clickFeedback\}/);
	assert.equal((popoverSource.match(/data-testid="refresh-click-feedback"/g) ?? []).length, 1);
	assert.equal((refresh.match(/data-testid="refresh-click-feedback"/g) ?? []).length, 0, 'rendered only by the popover');
});

test('every failed click records its own outcome, and each click starts from a clean slate', () => {
	const click = refresh.slice(refresh.indexOf('async function onClick()'), refresh.indexOf('onDestroy('));
	assert.match(click, /^async function onClick\(\): Promise<void> \{\s*clickFeedback = null;/);
	const failure = click.slice(click.indexOf('} catch (e) {'));
	for (const outcome of ["'Already running'", "'WIP - not working: no ingestion steps enabled'", '`WIP - not working: ${e.message}`']) {
		assert.ok(failure.includes(`clickFeedback = ${outcome}`), outcome);
	}
	assert.doesNotMatch(click, /hovered = true;/, 'opening without onEnter starts no polling');
});

// Codex P2 4130152643 and 4130407830: every click, started (202) or refused,
// opens the popover the way a hover does (polling included), and only when
// not already open. 4130581613: it opens BEFORE the POST is awaited, never
// after it, so a dismissal while the POST is pending is not undone when it
// settles. A started run still polls while it lasts (_syncTimer after the
// POST), which is how its badges and done toast arrive. Driven for real in
// tests/e2e/refresh-analysis-keyboard-409.spec.ts.
test('every click opens the popover once, before the POST, whatever the outcome', () => {
	const click = refresh.slice(refresh.indexOf('async function onClick()'), refresh.indexOf('onDestroy('));
	assert.equal((click.match(/void onEnter\(\)/g) ?? []).length, 1);
	const openAt = click.indexOf('if (!hovered) void onEnter();');
	assert.ok(openAt > 0 && openAt < click.indexOf('await startIngestRefresh()'), 'opened before the POST');
	const started = click.slice(click.indexOf('await startIngestRefresh()'), click.indexOf('} catch (e) {'));
	assert.match(started, /_syncTimer\(\);/, 'a started run polls while it lasts');
});

test('a keyboard-opened popover closes without a mouse: blur and Escape', () => {
	const buttonStart = refresh.lastIndexOf('<button', refresh.indexOf('data-testid="refresh-analysis"'));
	const button = refresh.slice(buttonStart, refresh.indexOf('>', refresh.indexOf('data-testid="refresh-analysis"')));
	assert.match(button, /onblur=\{onBlur\}/);
	assert.match(button, /onkeydown=\{onKeydown\}/);
	assert.match(refresh, /function onBlur\(\): void \{\s*if \(hovered && !wrapEl\?\.matches\(':hover'\)\) onLeave\(\);/);
	assert.match(refresh, /if \(e\.key === 'Escape' && hovered\) onLeave\(\);/);
});

test('a real running status still renders its progress bar alongside any click result (control)', () => {
	const statusAt = popover.indexOf("{#if status !== null && status.phase !== 'idle'}");
	const progress = popover.slice(statusAt, popover.indexOf('{:else', statusAt));
	assert.match(progress, /class="bar"/);
	assert.match(progress, /status\.step_done/);
	// The status block is not gated on clickFeedback, so a 409 'Already
	// running' click shows the live progress too.
	assert.doesNotMatch(popover.slice(0, statusAt), /\{#if clickFeedback === null\}/);
});

// Codex P2 4130868861 and 4131292228: a status answer delivered out of order
// (the pre-start idle one after the POST, or a running one after its run's
// end) must not replace a newer one. Codex P2 4132045411: a no-job snapshot
// asked for after the shown one arrived is a restarted backend, and applies.
// Driven end to end in tests/e2e/refresh-analysis-keyboard-409.spec.ts.
test('a polled snapshot older than the one shown is dropped before it is applied', () => {
	const poll = refresh.slice(refresh.indexOf('async function _poll()'), refresh.indexOf('function _syncTimer()'));
	assert.match(poll, /const asked = status;\s*try \{/);
	assert.match(
		poll,
		/const s = await getIngestRefreshStatus\(\);[\s\S]*?if \(!refreshSnapshotApplies\(status, s, asked === status\)\) return;\s*const wasRunning = status\?\.running === true;\s*status = s;/
	);
	// The start POST's answer replaces the shown snapshot, so a GET that raced
	// it reads as asked before it and stays stale.
	assert.match(refresh, /status = await startIngestRefresh\(\);/);
});

test('snapshot order: stale answers drop, newer ones and a restarted backend apply', async () => {
	const { refreshSnapshotApplies: applies } = await loadTypeScriptModule('src/lib/rb/refresh-status-order.ts');
	const idle = { started_at: null, finished_at: null };
	const running = { started_at: 100, finished_at: null };
	const ended = { started_at: 100, finished_at: 200 };
	// [if] the idle answer to a GET that raced the start POST lands after it
	// [then] it is dropped, so the run keeps being followed, [else stop].
	assert.equal(applies(running, idle, false), false);
	// [if] the backend restarts mid-run and a later poll reads no job [then]
	// that is applied, so the spinner stops, [else stop].
	assert.equal(applies(running, idle, true), true);
	// A run's earlier running snapshot never follows its end, asked early or late.
	assert.equal(applies(ended, running, false), false);
	assert.equal(applies(ended, running, true), false);
	// Controls: newer snapshots of the same or a later run apply, and so does
	// the first answer when nothing is shown yet.
	assert.equal(applies(running, ended, false), true);
	assert.equal(applies(running, { started_at: 100, finished_at: null }, false), true);
	assert.equal(applies(ended, { started_at: 300, finished_at: null }, false), true);
	assert.equal(applies(null, idle, false), true);
	assert.equal(applies(null, running, false), true);
});

// ------------------------------------------------ lazy popover (issue #3886)
// The popover exists only after a hover or click, so a static import would put
// its markup back in the /performance initial chunks (bundle budget).

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

function sourceFiles(dir) {
	return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
		const path = join(dir, e.name);
		if (e.isDirectory()) return sourceFiles(path);
		return /\.(svelte|ts|js)$/.test(e.name) ? [path] : [];
	});
}

// Any static import form naming the popover: default, named, namespace,
// type-only or bare side-effect. A dynamic import( ... ) has a paren instead.
const STATIC_POPOVER_IMPORT = /(^|[;\s])import\s+(?:type\s+)?(?:[\w$*{},\s]+\s+from\s+)?['"][^'"]*RefreshAnalysisPopover(?:\.svelte)?['"]/m;
const DYNAMIC_POPOVER_IMPORT = /\bimport\(\s*['"]\.\/RefreshAnalysisPopover\.svelte['"]\s*\)/;

test('the probe for a static popover import fires on every static form and not on the dynamic one (control)', () => {
	for (const form of [
		"import RefreshAnalysisPopover from './RefreshAnalysisPopover.svelte';",
		"\timport Popover from '$lib/components/rb/RefreshAnalysisPopover.svelte';",
		"import type { X } from './RefreshAnalysisPopover.svelte';",
		"import * as P from './RefreshAnalysisPopover.svelte';",
		"import './RefreshAnalysisPopover.svelte';"
	]) {
		assert.match(form, STATIC_POPOVER_IMPORT, form);
	}
	assert.doesNotMatch("import('./RefreshAnalysisPopover.svelte')", STATIC_POPOVER_IMPORT);
});

test('no module statically imports the refresh popover; the button imports it dynamically', () => {
	const files = sourceFiles(SRC);
	assert.ok(files.length > 100, `walked ${files.length} source files, expected the whole src tree`);
	const offenders = files.filter((f) => STATIC_POPOVER_IMPORT.test(readFileSync(f, 'utf8')));
	assert.deepEqual(offenders, []);
	// Control: the one import that should exist does, in the button.
	assert.match(refresh, DYNAMIC_POPOVER_IMPORT);
});

test('compiled, the button has no static popover import and one dynamic import of it', () => {
	const { js } = compile(refresh, { filename: 'RefreshAnalysisButton.svelte', generate: 'client' });
	const staticImports = [...js.code.matchAll(/^import[^\n]*$/gm)].map((m) => m[0]);
	assert.ok(staticImports.length > 0, 'the compiled module has its static imports');
	assert.deepEqual(staticImports.filter((l) => l.includes('RefreshAnalysisPopover')), []);
	assert.equal((js.code.match(DYNAMIC_POPOVER_IMPORT) ?? []).length, 1);
});

test('the popover renders and loads only while open, so a dismissal during the load stays made', () => {
	const effect = refresh.slice(refresh.indexOf('$effect(() => {'), refresh.indexOf('function _applyBadges'));
	assert.match(effect, /if \(!hovered \|\| popoverRequested\) return;\s*popoverRequested = true;/);
	assert.match(effect, DYNAMIC_POPOVER_IMPORT);
	// Fetched once per document: the failure path never clears the latch.
	assert.doesNotMatch(effect, /popoverRequested = false/);
	const markup = refresh.slice(refresh.lastIndexOf('</script>'));
	assert.match(
		markup,
		/\{#if hovered && Popover !== null\}\s*<Popover \{wrapEl\} \{status\} \{coverage\} \{config\} \{queue\} \{fetchError\} \{clickFeedback\} \/>/
	);
	// Every dismissal clears hovered: mouseleave, blur and Escape all end in onLeave.
	assert.match(markup, /onmouseleave=\{onLeave\}/);
	assert.match(refresh, /function onLeave\(\): void \{\s*hovered = false;/);
});

test('a failed popover import is shown inline with its error text, a Reload and a Close', () => {
	const effect = refresh.slice(refresh.indexOf('$effect(() => {'), refresh.indexOf('function _applyBadges'));
	assert.match(effect, /\.catch\(\(exc: unknown\) => \{[\s\S]*?popoverLoadError = exc instanceof Error \? exc\.message : String\(exc\);/);
	const markup = refresh.slice(refresh.lastIndexOf('</script>'));
	const branch = markup.slice(markup.indexOf('{:else if hovered && popoverLoadError !== null}'), markup.indexOf('{/if}'));
	assert.ok(branch.length > 0, 'the error branch exists and is gated on hovered');
	assert.match(branch, /role="alert"/);
	assert.match(branch, /failed to load: \{popoverLoadError\}/);
	assert.match(branch, /onclick=\{\(\) => location\.reload\(\)\}>Reload<\/button>/);
	assert.match(branch, /onclick=\{onLeave\}>Close<\/button>/);
});

// HEALTH-12: on the silver preview (Mon 5 Oct 2026) an uncached coverage read
// took 43 to 60 s, so every hover started a whole-library measure of its own.
test('a hover reads the cached coverage; only a finished refresh measures afresh', () => {
	// [if] the pointer enters the button [then] coverage is read cached, [else stop].
	const enter = refresh.slice(refresh.indexOf('async function onEnter()'), refresh.indexOf('function onLeave()'));
	assert.match(enter, /getIngestCoverage\(\{ cached: true \}\)/);
	assert.doesNotMatch(enter, /getIngestCoverage\(\)/);
	const poll = refresh.slice(refresh.indexOf('async function _poll()'), refresh.indexOf('function _syncTimer()'));
	assert.match(poll, /getIngestCoverage\(\)/, 'a finished refresh changes the counts, so it measures');
});
