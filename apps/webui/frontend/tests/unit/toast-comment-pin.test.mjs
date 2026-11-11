/**
 * @pytest.mark.requirement FEED-12632
 * Pin 30de7a76291f: after a toast copy, M placement prefills the pin with the report.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (p) => readFileSync(fileURLToPath(new URL(`../../${p}`, import.meta.url)), 'utf8');

let ctx;

before(async () => {
	ctx = await loadTypeScriptModule('src/lib/toast-copy-context.ts');
});

afterEach(() => {
	ctx.clearToastCommentPinArm();
});

test('[if] a later toast is copied [then] M arms the latest report, [else stop].', () => {
	ctx.armToastCommentPin('t-a-1', 'first report');
	ctx.armToastCommentPin('t-b-2', 'second report');
	const armed = ctx.peekToastCommentPinArm();
	assert.equal(armed?.logId, 't-b-2');
	assert.equal(armed?.reportText, 'second report');
});

test('[if] placement consumes the arm [then] the draft text is the report once, [else stop].', () => {
	ctx.armToastCommentPin('t-c-3', 'pin body');
	assert.equal(ctx.takeToastCommentPinDraftText(), 'pin body');
	assert.equal(ctx.peekToastCommentPinArm(), null);
});

test('[if] copyToast succeeds [then] it arms the toast comment pin, [else stop].', () => {
	const stores = read('src/lib/stores.svelte.ts');
	const copyAt = stores.indexOf('export async function copyToast');
	assert.ok(copyAt > 0);
	const copyBody = stores.slice(copyAt, stores.indexOf('export const health', copyAt));
	assert.match(copyBody, /armToastCommentPin\(toast\.logId, text, TOAST_COPIED_NOTE_MS\)/);
});
