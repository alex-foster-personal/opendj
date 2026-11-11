/**
 * BL conn-dot tips (#11172): per-dot meaning, ask-agent prompts, copy-all.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const panelPath = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);
const stripPath = fileURLToPath(
	new URL('../../src/lib/components/rb/ConnDotsStrip.svelte', import.meta.url)
);

const tips = await loadTypeScriptModule('src/lib/rb/conn-dot-tips.ts');
const health = await loadTypeScriptModule('src/lib/rb/library-health-dots.ts');

// [if] red/gray conn dot [then] ask-agent prompt exists, [else stop].
test('conn tips include ask-agent lines when a bright dot is down or unknown', () => {
	const down = tips.connTipFromHealthDot({
		label: 'Backend',
		state: 'error',
		detail: 'engine not answering'
	});
	assert.ok(down?.askAgent?.length, 'error backend must ship ask-agent prompts');
	const unknown = tips.connTipFromHealthDot({
		label: 'Library health',
		state: 'unavailable',
		detail: 'unknown - timed out'
	});
	assert.ok(unknown?.askAgent?.length, 'gray library must ship ask-agent prompts');
	const up = tips.connTipFromHealthDot({
		label: 'Frontend',
		state: 'complete',
		detail: 'dev server online'
	});
	assert.equal(up?.askAgent, undefined, 'green dots must not invent ask-agent lines');
});

// [if] copy-all runs [then] clipboard text includes every tip and policy, [else stop].
test('formatCopyAllTips includes the policy line and every dot block', () => {
	const all = [
		tips.softDotTip({ id: 'spotify', label: 'Spotify', up: false, detail: 'not signed in' }),
		tips.connTipFromHealthDot({ label: 'Backend', state: 'error', detail: 'down' })
	];
	const text = tips.formatCopyAllTips(all);
	assert.match(text, /Nothing leaves this machine until you submit/);
	assert.match(text, /Spotify \(soft\)/);
	assert.match(text, /BE \(backend API\)/);
	assert.match(text, /Ask agent:/);
});

// [if] health state is error/unavailable [then] connDotNeedsAskPrompt is true, [else stop].
test('library-health-dots connDotNeedsAskPrompt matches red and gray only', () => {
	assert.equal(health.connDotNeedsAskPrompt('error'), true);
	assert.equal(health.connDotNeedsAskPrompt('unavailable'), true);
	assert.equal(health.connDotNeedsAskPrompt('complete'), false);
});

// [if] soft dot is off [then] tip is off with ask lines, [else stop].
test('softDotTip adds ask-agent prompts when a soft dependency is off', () => {
	const tip = tips.softDotTip({
		id: 'rekordbox',
		label: 'Rekordbox',
		up: false,
		detail: 'no key'
	});
	assert.equal(tip.status, 'off');
	assert.ok(tip.askAgent?.some((line) => line.includes('rekordbox')));
});

test('ConnDotsStrip exposes pin, copy-all, policy, and keyboard close', () => {
	const src = readFileSync(stripPath, 'utf8');
	assert.match(src, /data-testid="conn-dot-pin"/);
	assert.match(src, /data-testid="conn-dot-copy-all"/);
	assert.match(src, /data-testid="conn-dot-policy"/);
	assert.match(src, /data-custom-tip=""/);
	assert.match(src, /e\.key === 'Escape'/);
	assert.doesNotMatch(src, /openGithubDraft/);
});

test('BrowserPanel mounts ConnDotsStrip with live health dots', () => {
	const src = readFileSync(panelPath, 'utf8');
	assert.match(src, /import ConnDotsStrip from '\.\/ConnDotsStrip\.svelte'/);
	assert.match(
		src,
		/<ConnDotsStrip libDot=\{libraryHealth\} beDot=\{backendOnline\} feDot=\{frontendOnline\}/
	);
});
