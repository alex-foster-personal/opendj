import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Issue #13232 ((private path), Verification).
// A shared engine process is counted once in the memory figure, and the page
// term is added at most once: a renderer-owned term never duplicates it.
//
// The browser-tab and Electron-renderer cases are pinned in
// memory-meter-model.test.mjs (#9318). Regression line:
// - if process metadata is incomplete (no role key, an unreadable renderer,
//   an unknown shell) then the page term is kept once, never dropped and never
//   doubled

let model;

before(async () => {
	model = await loadTypeScriptModule('src/lib/rb/memory-meter-model.ts');
});

const PCM_ONLY = {
	jsHeapMB: null,
	pcmMB: 485,
	anlzMB: 12,
	anlzCount: 10,
	prefetchMB: 30,
	prefetchCount: 3
};

const ENGINE = { label: 'opendj-engine', mb: 600, role: null };
const RENDERER = { label: 'unnamed', mb: 700, role: 'electron-renderer' };

test('members without a role key keep one page term under the Electron shell', () => {
	// An engine that does not report roles cannot name the renderer, so the page term stays.
	const noRole = [
		{ label: 'opendj-engine', mb: 600 },
		{ label: 'unnamed', mb: 700 }
	];
	const out = model.memoryReadout({
		...PCM_ONLY,
		jsHeapMB: 268,
		processes: noRole,
		shellKind: 'electron'
	});
	assert.equal(model.measuredPageProcess(noRole, 'electron'), null);
	assert.equal(out.totalMB, 1300 + 268 + 485);
	assert.doesNotMatch(out.hover, /this page measured in its own process/);
});

test('an unreadable renderer under Electron keeps one page term and sums nothing for it', () => {
	// mb null is not a measurement, so it cannot stand in for the page.
	const members = [ENGINE, { ...RENDERER, mb: null }];
	const out = model.memoryReadout({
		...PCM_ONLY,
		jsHeapMB: 268,
		processes: members,
		shellKind: 'electron'
	});
	assert.equal(model.measuredPageProcess(members, 'electron'), null);
	assert.equal(out.processesMB, 600, 'the unreadable renderer is not summed as 0 or as anything');
	assert.equal(out.totalMB, 600 + 268 + 485, 'the page term is added once');
	assert.match(out.hover, /could not be read - NOT in the figure above/);
	assert.doesNotMatch(out.hover, /this page measured in its own process/);
});

test('an unknown shell keeps the legacy WebContent match so the page is not counted twice', () => {
	// shellKind omitted with a measured webkit-webcontent member: legacy samples treat it as the page.
	const webcontent = { label: 'opendj-webcontent', mb: 900, role: 'webkit-webcontent' };
	const out = model.memoryReadout({ ...PCM_ONLY, processes: [ENGINE, webcontent] });
	assert.equal(model.measuredPageProcess([ENGINE, webcontent]), webcontent);
	assert.equal(out.totalMB, 1500, 'the WebContent footprint already holds the page');
	// control: an explicit browser tab adds the page term once
	const tab = model.memoryReadout({
		...PCM_ONLY,
		processes: [ENGINE, webcontent],
		shellKind: null
	});
	assert.equal(tab.totalMB, 1500 + 485);
});
