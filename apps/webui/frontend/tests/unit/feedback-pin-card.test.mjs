/**
 * The comment pin is a control you can open, not a native tooltip.
 *
 * Pin c8d29afa994d (the maintainer, Wed 2 Sep 2026): "can't click on comments to re-open
 * them / add furhter comemnts. Hover > see comment is slow and built-in. 500ms
 * debounce." And pin 307e0e84bfbe: "half-written comments are lost on refresh
 * - auto-save so they aren't."
 *
 * Both are bugs in the review loop itself, which is why they come first: a
 * review tool that loses what you typed, and whose replies can only be read
 * through a slow truncating browser tooltip, costs more than the bugs it finds.
 *
 * `.fb-pin` was a <span> carrying a native `title`, so it could not be clicked,
 * could not be focused, and rendered through the browser's own ~1 s tooltip.
 * That tooltip is also where an agent's triage note has to be read, and those
 * run to a paragraph.
 *
 * The draft half is pure and testable; the markup half is source-shape, the
 * same way capability-gating-markup.test.mjs pins its facts.
 *
 * Markup NOTE: the pin marker and the reopened pin body were later split out
 * of FeedbackWidget.svelte into FeedbackPinMarkers.svelte (the `.fb-pin`
 * button, #858) and FeedbackPinCard.svelte (the `.fb-pin-body` dialog,
 * pin 7f4f3a903343 / issue #928) - the markup assertions below follow that
 * split rather than re-testing FeedbackWidget.svelte directly.
 *
 * Regression lines:
 * - if the pin goes back to a native-only title then the agent note is
 *   unreadable and the hover is slow again
 * - if the pin stops being a button then it is unclickable and unfocusable
 * - if a draft is not restored then half-written comments are lost on refresh
 *   for the second time
 * - if a SAVED pin's text ends up in client storage then feedback content has
 *   started living somewhere an agent harvest cannot see
 * - if a reopened pin card dismisses on ANY outside press (not just a
 *   reopened one) or loses its close X, pin 7f4f3a903343 regresses
 * - if the reopened card stops repositioning from its own measured size,
 *   issue #928 regresses
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

// FeedbackPinCard's mount point, openPinId/closePin state, and the
// pinDraft-retention rule moved from FeedbackWidget into the app-root pin
// layer (FB-16, #3888).
const pinLayer = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/FeedbackPinLayer.svelte', import.meta.url)),
	'utf8'
);
const markers = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/feedback/FeedbackPinMarkers.svelte', import.meta.url)),
	'utf8'
);
const card = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/FeedbackPinCard.svelte', import.meta.url)),
	'utf8'
);

let mod;
before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/feedback.ts');
});

// ---------------------------------------------------------------- markup

test('the pin marker is a focusable button, not a span', () => {
	const at = markers.indexOf('class="fb-pin ');
	assert.ok(at > 0, 'no .fb-pin in FeedbackPinMarkers');
	const open = markers.lastIndexOf('<', at);
	assert.ok(
		markers.slice(open, at).includes('button'),
		'the comment pin is not a button, so it cannot be clicked or focused'
	);
	assert.match(markers, /onclick=\{\(\) => onopen\(pin\)\}/);
});

test('the reopened card has a follow-up composer wired through pinThread', () => {
	assert.match(card, /pinThread/);
	assert.match(card, /class="fb-reply"/);
	assert.match(card, /aria-label="Follow-up comment"/);
	assert.match(card, /aria-label="Add follow-up comment"/);
	assert.doesNotMatch(card, /\/follow-on/, 'the reply composer must not call follow-on');
});

test('the pin opens a real dialog, not just a native title', () => {
	// #858 kept a native `title` as a quick hover preview, but the full text
	// is also reachable through a clickable, selectable body (fb-pin-body) -
	// the native-title-only failure this guards against is a pin whose ONLY
	// way to read its content is that slow, truncating, unselectable tooltip.
	assert.match(card, /class="fb-pin-body"/);
	assert.match(card, /role="dialog"/);
});

test('a reopened pin card has a close X and only outside pointer presses dismiss it', () => {
	assert.match(pinLayer, /import FeedbackPinCard from '.\/FeedbackPinCard\.svelte'/);
	assert.match(pinLayer, /<FeedbackPinCard[\s\S]*onclose=\{closePin\}/);
	assert.match(card, /aria-label="Close comment pin"/);
	assert.match(card, /onclick=\{onclose\}>×<\/button/);
	assert.match(
		card,
		/<svelte:window[^>]*\bonpointerdowncapture=\{handleOutsidePinPointerDown\}/,
		'outside clicks must close the card before a resize handle stops bubbling'
	);
	const handler = card.slice(
		card.indexOf('function handleOutsidePinPointerDown'),
		card.indexOf('</script>')
	);
	assert.match(handler, /pinBodyElement\?\.contains\(target\)/);
	assert.match(handler, /onclose\(\)/);
	const close = pinLayer.slice(pinLayer.indexOf('function closePin'), pinLayer.indexOf('function archiveOpenPin'));
	assert.match(close, /openPinId = null/);
	assert.doesNotMatch(close, /pinDraft\s*=\s*null/, 'dismissing a reopened card must retain a new draft');
});

// Issue #928: the body hangs down-right of its marker, so a pin dropped near
// an edge must be nudged fully on-screen using its REAL measured size -
// reusing clampPanelPos (via pinBodyPos), not a fixed-size CSS guess.
test('the reopened card repositions from its own measured size, on mount and on resize', () => {
	assert.match(card, /import \{ pinBodyPos, pinBodyStyle, pinIsDone, pinStatus \} from '\$lib\/rb\/feedback'/);
	assert.match(card, /getBoundingClientRect\(\)/);
	// `point ?? pin`: the card follows the marker's resolved position
	// (feedback-pin-position.ts) and falls back to the pin's own percentages.
	assert.match(card, /pinBodyPos\(\s*\n?\s*point \?\? pin,/);
	assert.match(card, /<svelte:window[^>]*\bonresize=\{_reposition\}/);
	assert.match(card, /style=\{bodyStyle\}/, 'the div must render the measured position, not the fixed guess');
});

// ---------------------------------------------------------------- drafts

describe('pin draft persistence', () => {
	it('round-trips a draft through storage', () => {
		const { serializePinDraft, parsePinDraft } = mod;
		const draft = {
			point: { x_pct: 12.5, y_pct: 40 },
			anchor: '.c-bpm',
			text: 'half typed',
			page: '/performance'
		};
		const restored = parsePinDraft(serializePinDraft(draft));
		// No viewport/followOn in the stored draft means neither key comes back
		// at all - exactOptionalPropertyTypes forbids parsePinDraft from
		// returning them present-but-undefined, so absence is the correct shape,
		// not `undefined`-valued keys.
		assert.deepEqual(restored, draft);
		assert.equal('viewport' in restored, false, 'viewport key should be absent, not undefined');
		assert.equal('followOn' in restored, false, 'followOn key should be absent, not undefined');
	});

	it('round-trips a follow-on draft, parent link and viewport included', () => {
		const { serializePinDraft, parsePinDraft } = mod;
		const draft = {
			point: { x_pct: 12.5, y_pct: 40 },
			anchor: '.c-bpm',
			text: 'a reply',
			page: '/performance',
			viewport: { width: 1440, height: 900 },
			followOn: { parentId: 'abc123', label: 'parent text' }
		};
		assert.deepEqual(parsePinDraft(serializePinDraft(draft)), draft);
	});

	it('reads garbage as no draft rather than crashing the mount', () => {
		const { parsePinDraft } = mod;
		for (const junk of [
			null,
			'',
			'not json',
			'{}',
			'[]',
			'{"text":"x"}',
			'{"point":{"x_pct":"a","y_pct":1},"anchor":null,"text":"x","page":"/performance"}',
			// r3919185341: a draft with no page tag (or an empty one) cannot be
			// checked against the current pathname on restore, so it is junk
			// the same way a malformed point is.
			'{"point":{"x_pct":1,"y_pct":2},"anchor":null,"text":"x"}',
			'{"point":{"x_pct":1,"y_pct":2},"anchor":null,"text":"x","page":""}',
			'{"point":{"x_pct":1,"y_pct":2},"anchor":null,"text":"x","page":3}',
			// a corrupt follow-on link is worse than none - do not restore it
			'{"point":{"x_pct":1,"y_pct":2},"anchor":null,"text":"x","page":"/p","followOn":{"parentId":1}}'
		]) {
			assert.equal(parsePinDraft(junk), null, `accepted junk: ${String(junk)}`);
		}
	});

	it('treats an empty draft as nothing to restore', () => {
		const { parsePinDraft, serializePinDraft } = mod;
		const empty = { point: { x_pct: 1, y_pct: 2 }, anchor: null, text: '   ', page: '/performance' };
		assert.equal(parsePinDraft(serializePinDraft(empty)), null);
	});

	it('keys storage separately from the panel position', () => {
		const { PIN_DRAFT_KEY, PANEL_POS_KEY } = mod;
		assert.equal(typeof PIN_DRAFT_KEY, 'string');
		assert.notEqual(PIN_DRAFT_KEY, PANEL_POS_KEY);
	});
});
