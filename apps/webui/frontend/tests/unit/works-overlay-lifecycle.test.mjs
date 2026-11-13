import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const FRONTEND_ROOT = fileURLToPath(new URL('../../', import.meta.url));
const WORKS_OVERLAY = readFileSync(
	join(FRONTEND_ROOT, 'src/lib/rb/works-overlay.svelte.ts'),
	'utf8'
);

test('startWorksOverlay fail returns when isPageGoingAway before pushToast', () => {
	const fnStart = WORKS_OVERLAY.indexOf('function startWorksOverlay()');
	assert.ok(fnStart >= 0);
	const slice = WORKS_OVERLAY.slice(fnStart, fnStart + 600);
	assert.match(slice, /if \(isPageGoingAway\(\)\) return;/);
	assert.match(slice, /works did not load/);
});

test('refreshSuggestionFlags catch returns when isPageGoingAway before pushToast', () => {
	const fnStart = WORKS_OVERLAY.indexOf('export function refreshSuggestionFlags');
	assert.ok(fnStart >= 0);
	const slice = WORKS_OVERLAY.slice(fnStart, fnStart + 650);
	assert.match(slice, /version suggestions failed/);
	assert.match(slice, /if \(isPageGoingAway\(\)\) return;/);
});
