/**
 * Issue #2073: feedback pin attachment thumbnails on marker and card, plus
 * lightbox wiring on the card. SSR proves the {#if pin.attachment} branches;
 * source-shape asserts pin the click-to-enlarge / Esc dismiss contract.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const CARD_PATH = `${SRC}/lib/components/rb/FeedbackPinCard.svelte`;

const ENTRY = [
	"export { default as Card } from '$lib/components/rb/FeedbackPinCard.svelte';",
	"export { default as Markers } from '$lib/components/rb/feedback/FeedbackPinMarkers.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

const ATT = {
	id: 'att1',
	content_type: 'image/png',
	size_bytes: 2048,
	url: '/api/v1/feedback/attachments/att1'
};

function pin(overrides = {}) {
	return {
		id: 'da24b74a8c38',
		x_pct: 40,
		y_pct: 40,
		anchor: null,
		page: '/performance',
		text: 'Air preview store',
		created_at: '2026-09-11T00:00:00Z',
		build: { git_sha: 'abc', built_at_utc: '2026-09-11T00:00:00Z', source: 'test' },
		status: 'open',
		attachment: null,
		...overrides
	};
}

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

test('FeedbackPinCard with attachment renders fb-attachment-img and url', () => {
	const html = mod.render(mod.Card, {
		props: {
			pin: pin({ attachment: ATT }),
			onclose() {},
			onarchive() {},
			onfollowon() {}
		}
	}).body;
	assert.match(html, /fb-attachment-img/);
	assert.match(html, new RegExp(ATT.url.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
});

test('FeedbackPinCard without attachment omits fb-attachment-img and fb-lightbox', () => {
	const html = mod.render(mod.Card, {
		props: {
			pin: pin(),
			onclose() {},
			onarchive() {},
			onfollowon() {}
		}
	}).body;
	assert.equal(html.includes('fb-attachment-img'), false);
	assert.equal(html.includes('fb-lightbox'), false);
});

// FB-23: the marker says a screenshot is attached WITHOUT fetching it. Every
// marker used to carry an <img> on the attachment url, so a page load asked
// for every pin's screenshot at once (8 requests, 6 of them 404s, measured
// Thu 1 Oct 2026 on a data dir that holds the pins but not their images).
//   - if a marker requests its attachment at load then broken
//   - if a marker with an attachment shows nothing for it then broken
test('FeedbackPinMarkers with attachment marks it without requesting it', () => {
	const html = mod.render(mod.Markers, {
		props: {
			pins: [pin({ attachment: ATT })],
			seen: {},
			onopen() {}
		}
	}).body;
	assert.match(html, /fb-pin-thumb/, 'the marker still shows that a screenshot is attached');
	assert.match(html, /Screenshot attached - open the pin to load it/);
	assert.equal(html.includes(ATT.url), false, 'the marker must not carry the attachment url');
	assert.equal(html.includes('<img'), false, 'the marker must not load any image');
});

test('control: the opened card is where the attachment is requested', () => {
	const html = mod.render(mod.Card, {
		props: { pin: pin({ attachment: ATT }), onclose() {}, onarchive() {}, onfollowon() {} }
	}).body;
	assert.match(html, /<img[^>]*fb-attachment-img/);
	assert.ok(html.includes(ATT.url));
});

test('a 404 on the opened card names the state instead of a broken image', () => {
	const text = readFileSync(CARD_PATH, 'utf8');
	assert.match(text, /onerror=\{\(\) => \(unsyncedAttachmentId = attachmentId\)\}/);
	assert.match(
		text,
		/unsyncedAttachmentId === pin\.attachment\.id\}[\s\S]{0,200}?fb-attachment-unsynced[\s\S]{0,120}?Attachment not on this machine/
	);
});

test('FeedbackPinMarkers without attachment omits fb-pin-thumb', () => {
	const html = mod.render(mod.Markers, {
		props: {
			pins: [pin()],
			seen: {},
			onopen() {}
		}
	}).body;
	assert.equal(html.includes('fb-pin-thumb'), false);
});

test('FeedbackPinCard source wires lightbox open, Esc capture, and outside-click guard', () => {
	const text = readFileSync(CARD_PATH, 'utf8');
	assert.match(text, /lightboxOpen/);
	// FBSYNC-05 (commit 2b6e5b745) and the lightbox/button feature (commit
	// f91ed1ab1) were merged together in 2e2d4c729 ("fix(sweep): round 1
	// (composer-2.5) for #1978", Fri 11 Sep 2026 20:59), which interposed an
	// unsynced-attachment branch ahead of the real thumbnail: the button now
	// lives under `{:else if pin.attachment}`, not the top `{#if pin.attachment}`.
	assert.match(
		text,
		/\{(?:#if|:else if) pin\.attachment\}[\s\S]{0,400}?<button[\s\S]{0,400}?fb-attachment-img/,
		'attachment thumbnail must be a button that opens the lightbox'
	);
	assert.match(text, /class="fb-lightbox"/);
	assert.match(text, /aria-label="Screenshot"/);
	assert.match(text, /aria-label="Close screenshot"/);
	assert.match(text, /onkeydowncapture/);
	assert.match(text, /stopImmediatePropagation/);
	assert.match(text, /Escape/);
	assert.match(text, /lightboxElement/);
	assert.match(
		text,
		/handleOutsidePinPointerDown[\s\S]{0,300}lightboxElement\?\.contains\(target\)/,
		'outside-click handler must treat the lightbox as inside the card'
	);
});
