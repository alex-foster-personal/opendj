/**
 * A Camelot title contains " - ". The on-screen headline must keep the whole
 * title and the plain sentence for the code. The copy detail keeps the code.
 *
 * @pytest.mark.requirement UX-TOAST-02
 */
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'http://127.0.0.1:65530';
const TITLE = '7A - 6 - Glasswing';
const PATH = '/Users/dev/Music/Library/Glasswing.aiff';
const ROW = 'e9f3abcd';

let harness;
let presentation;

before(async () => {
	harness = await loadTypeScriptModule('tests/unit/fixtures/stick-load-failure-toast-entry.ts', {
		viteApiBase: API_BASE
	});
	presentation = await loadTypeScriptModule('src/lib/toast-presentation.ts');
	globalThis.fetch = async () => new Response(null, { status: 204 });
});

beforeEach(() => {
	harness.toasts.length = 0;
});

function cloudDetail() {
	return `track_locations row '${ROW}' marked unavailable (available=0) at '${PATH}'`;
}

test('a title containing " - " stays whole in the headline, and the copy keeps the code', () => {
	const cause = new harness.RbApiError(404, 'CLOUD_ASSET_UNAVAILABLE', cloudDetail());
	const loadMessage = `${TITLE}: ${cause.message}`;
	harness.reportDeckLoadFailure(2, loadMessage, cause, {}, {});
	const toast = harness.toasts.at(-1);
	assert.match(toast.headline, /couldn't load "7A - 6 - Glasswing"/);
	assert.match(toast.headline, /Its audio isn't on this Mac/);
	assert.match(toast.headline, /\/Users\/dev\/Music\/Library\/Glasswing\.aiff/);
	assert.equal(toast.headline.includes('CLOUD_ASSET_UNAVAILABLE'), false);
	assert.equal(toast.headline.includes(ROW), false);
	assert.match(toast.message, /CLOUD_ASSET_UNAVAILABLE/);
	assert.match(toast.detail, /CLOUD_ASSET_UNAVAILABLE/);
	assert.match(toast.detail, new RegExp(ROW));
	assert.match(toast.detail, /\/Users\/dev\/Music\/Library\/Glasswing\.aiff/);
});

test('every library failure code stays out of the headline when the title contains " - "', () => {
	const codes = [
		'TRACK_NOT_FOUND',
		'AUDIO_FILE_MISSING',
		'CLOUD_ASSET_UNAVAILABLE',
		'CLOUD_POLICY_UNCONFIGURED',
		'AUDIO_ACCESS_BLOCKED'
	];
	for (const code of codes) {
		const cause = new harness.RbApiError(404, code, `server detail for ${code}`);
		harness.reportDeckLoadFailure(2, `${TITLE}: ${cause.message}`, cause, {}, {});
		const toast = harness.toasts.at(-1);
		assert.match(toast.headline, /couldn't load "7A - 6 - Glasswing"/, code);
		assert.equal(toast.headline.includes(code), false, code);
		assert.match(toast.message, new RegExp(code), code);
		assert.match(toast.detail, new RegExp(code), code);
	}
});

test('a plain Error whose message carries the code still maps, and " - " is not a cut point', () => {
	const raw = `${TITLE}: CLOUD_ASSET_UNAVAILABLE: ${cloudDetail()}`;
	const formatted = presentation.formatToastPresentation({ kind: 'error', message: raw });
	assert.match(formatted.headline, /couldn't load "7A - 6 - Glasswing"/);
	assert.equal(formatted.headline.includes('CLOUD_ASSET_UNAVAILABLE'), false);
	assert.equal(formatted.headline.startsWith('Open DJ'), false);
	assert.match(formatted.detail, /CLOUD_ASSET_UNAVAILABLE/);

	const cause = new Error(raw);
	harness.reportDeckLoadFailure(2, raw, cause, {}, {});
	const toast = harness.toasts.at(-1);
	assert.match(toast.headline, /Deck 2: couldn't load "7A - 6 - Glasswing"/);
	assert.equal(toast.headline.includes('CLOUD_ASSET_UNAVAILABLE'), false);
	assert.match(toast.message, /CLOUD_ASSET_UNAVAILABLE/);
});
