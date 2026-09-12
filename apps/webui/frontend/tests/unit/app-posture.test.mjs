// requirement: PERFMODE-03
// - if Gig posture is active then prefetch caps floor at 2 tracks even on STANDARD tier

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, '../../../../..');

test('shouldRunLibraryFallbackPoll respects bus open and posture intervals', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/app-posture.ts');
	const { shouldRunLibraryFallbackPoll, setResolvedPosture, GIG_LIBRARY_POLL_MS, PREP_LIBRARY_POLL_MS } =
		mod;
	setResolvedPosture('prep');
	assert.equal(shouldRunLibraryFallbackPoll(0, 0, true), false);
	assert.equal(shouldRunLibraryFallbackPoll(PREP_LIBRARY_POLL_MS, 0, false), true);
	assert.equal(shouldRunLibraryFallbackPoll(PREP_LIBRARY_POLL_MS - 1, 0, false), false);
	setResolvedPosture('gig');
	assert.equal(shouldRunLibraryFallbackPoll(PREP_LIBRARY_POLL_MS, 0, false), false);
	assert.equal(shouldRunLibraryFallbackPoll(GIG_LIBRARY_POLL_MS, 0, false), true);
	setResolvedPosture('prep');
});

test('prefetchTrackCapForPosture floors Gig at 2 tracks', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/app-posture.ts');
	const { prefetchTrackCapForPosture, prefetchByteCapForPosture, setResolvedPosture, GIG_PREFETCH_BYTES } =
		mod;
	setResolvedPosture('gig');
	assert.equal(prefetchTrackCapForPosture(4), 2);
	assert.equal(prefetchByteCapForPosture(48 * 1024 * 1024), GIG_PREFETCH_BYTES);
	setResolvedPosture('prep');
	assert.equal(prefetchTrackCapForPosture(4), 4);
});

test('BrowserPanel uses shouldRunLibraryFallbackPoll without hardcoded poll constant', () => {
	const source = readFileSync(
		join(REPO_ROOT, 'apps/webui/frontend/src/lib/components/rb/BrowserPanel.svelte'),
		'utf8'
	);
	assert.match(source, /shouldRunLibraryFallbackPoll/);
	assert.doesNotMatch(source, /LIBRARY_FALLBACK_POLL_MS = 60_000/);
});

test('AppPostureChip does not use Performance mode or Practice mode strings', () => {
	const source = readFileSync(
		join(REPO_ROOT, 'apps/webui/frontend/src/lib/components/rb/AppPostureChip.svelte'),
		'utf8'
	);
	assert.doesNotMatch(source, /Practice mode|Performance mode/);
	assert.match(source, /Prep/);
	assert.match(source, /Gig/);
});
