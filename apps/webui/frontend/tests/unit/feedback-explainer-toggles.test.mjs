/**
 * Pin 88e3abec02a0: the main comment/feedback button gets ONE shared
 * hover/focus explainer (ControlExplainer, the same component already used
 * to teach the /performance controls), worded for an end user, plus two
 * toggles: a working "show feedback comment pins" (default OFF for a new
 * viewer, persisted per-viewer) and a stubbed "show other users' pins"
 * (rb-inert, and a performance-bus command that answers not_implemented).
 *
 * Pin 6af63c5e9b7c: the hover-count breakdown work is ALREADY on main
 * (describePinStatusSummary, wired into commentPinTitle) - this packet only
 * adds the honest labelling of the buckets the comment API does not track.
 *
 * This is source-text wiring: the pieces of behaviour a Svelte compile step
 * would be needed to actually run are asserted here the same way
 * feedback-pin-card.test.mjs already does for this file.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const WIDGET = readFileSync(
	new URL('../../src/lib/components/rb/FeedbackWidget.svelte', import.meta.url),
	'utf8'
);
// pagePins gating, the __mdtPinsVisible programmatic twin, and the
// agent-pins filter moved out of FeedbackWidget into the app-root pin
// layer (FB-16, #3888) - the toggle button stays in the widget, but what
// it actually gates now lives here.
const PIN_LAYER = readFileSync(
	new URL('../../src/lib/components/rb/FeedbackPinLayer.svelte', import.meta.url),
	'utf8'
);
const EXPLAINER = readFileSync(
	new URL('../../src/lib/components/rb/deck/ControlExplainer.svelte', import.meta.url),
	'utf8'
);
const VISIBILITY_ACTIONS = readFileSync(
	new URL('../../src/lib/components/rb/FeedbackPinVisibilityActions.svelte', import.meta.url),
	'utf8'
);
const VISIBILITY_PREFERENCE = readFileSync(
	new URL('../../src/lib/rb/feedback-pin-visibility.ts', import.meta.url),
	'utf8'
);

test('the widget reuses the SAME shared explainer component the /performance controls use, not a second one', () => {
	assert.match(
		WIDGET,
		/import ControlExplainer from '\.\/deck\/ControlExplainer\.svelte'/,
		'must import the existing ControlExplainer rather than inventing a new popover'
	);
	// Sanity: ControlExplainer itself must still be the hover/focus teaching
	// chrome this test assumes it is - if it changes shape, this guard is stale.
	assert.match(EXPLAINER, /Hover\/focus teaching chrome for performance controls/);
});

test('the main comment button is wrapped in ControlExplainer with an end-user-worded explainer', () => {
	const btnAt = WIDGET.indexOf('aria-label="Drop a comment pin"');
	assert.notEqual(btnAt, -1);
	const before = WIDGET.slice(Math.max(0, btnAt - 700), btnAt);
	assert.match(before, /<ControlExplainer/, 'the comment button must be wrapped in the explainer');
	assert.match(
		WIDGET,
		/Give feedback,? ideas,? and suggestions to the developer,? and track them in-app/i,
		'the explainer heading must be worded for an end user, in the maintainer\'s own phrasing'
	);
});

test('the explainer bullets name the honest breakdown, including what is NOT tracked yet', () => {
	assert.match(WIDGET, /describePinStatusSummary\(feedbackState\.pins\)/);
	assert.match(
		WIDGET,
		/[Dd]elegated.*in-progress.*queued.*not tracked by the comment API/,
		'the unavailable buckets must be stated plainly, never silently omitted'
	);
});

test('a working, per-viewer "show feedback comment pins" toggle defaults new viewers OFF', () => {
	assert.match(WIDGET, /import\s*\{[^}]*readPinsVisible[^}]*\}\s*from\s*'\$lib\/rb\/feedback-pin-visibility'/s);
	assert.match(VISIBILITY_PREFERENCE, /parsePinsVisible/);
	assert.match(VISIBILITY_PREFERENCE, /serializePinsVisible/);
	assert.match(
		WIDGET,
		/pinsVisible(?:\s*:\s*boolean)?\s*=\s*\$state\(false\)/,
		'must default OFF before the stored value loads'
	);
	// The visible pins on the canvas must actually be gated by the toggle -
	// otherwise the checkbox is decorative and every viewer still sees pins.
	const pagePinsAt = PIN_LAYER.indexOf('const pagePins = $derived(');
	assert.notEqual(pagePinsAt, -1);
	const pagePinsBody = PIN_LAYER.slice(pagePinsAt, pagePinsAt + 250);
	assert.match(pagePinsBody, /pinsVisible/, 'pagePins must be gated on pinsVisible, or the toggle does nothing');
});

test('the "show feedback comment pins" toggle has an agent-facing programmatic twin, like pinSeen does', () => {
	assert.match(PIN_LAYER, /__mdtPinsVisible/, 'the pins-visible preference lives only in the browser, so it needs a twin');
});

test('the topbar pins checkbox follows writes from the twin and the M reveal, not just its mount read', () => {
	// FeedbackPinLayer writes the preference too (M reveal, __mdtPinsVisible.set);
	// a widget that only reads on mount shows a stale checkbox after either.
	assert.match(WIDGET, /import\s*\{[^}]*\bonPinsVisibleChanged\b[^}]*\}\s*from\s*'\$lib\/rb\/feedback-pin-visibility'/s);
	const onMountAt = WIDGET.indexOf('onMount(() => {');
	assert.notEqual(onMountAt, -1);
	const onMountBody = WIDGET.slice(onMountAt, WIDGET.indexOf('});', onMountAt));
	assert.match(onMountBody, /onPinsVisibleChanged\(\s*syncPinsVisible\s*\)/, 'the widget must subscribe while mounted');
	assert.match(onMountBody, /return\s*\(\)\s*=>\s*\{[\s\S]*offPinsVisibleChanged\(\)/, 'and unsubscribe on destroy');
	assert.match(WIDGET, /function syncPinsVisible\(\): void \{\s*pinsVisible = readPinsVisible\(window\.localStorage\);/);
});

test('agent pins have a topbar visibility toggle backed by the HTTP ui-prefs preference', () => {
	assert.match(WIDGET, /show_agent_pins/);
	assert.match(WIDGET, /setShowAgentPins/);
	assert.match(VISIBILITY_ACTIONS, /Show agent pins/);
	// #3790 rewrote the filter as an early return; either spelling is the same
	// gate. The predicate itself moved into PIN_LAYER's pagePins derivation
	// alongside the rest of the pin-visibility gating (FB-16, #3888).
	assert.match(
		PIN_LAYER,
		/p\.author !== 'agent' \|\| uiPrefs\.show_agent_pins|p\.author === 'agent' && !uiPrefs\.show_agent_pins/
	);
});

test('"show other users\' pins" is stubbed: rb-inert, explains why, never actually toggles', () => {
	assert.match(WIDGET, /import FeedbackPinVisibilityActions from '\.\/FeedbackPinVisibilityActions\.svelte'/);
	assert.match(WIDGET, /<FeedbackPinVisibilityActions/);
	const stubAt = VISIBILITY_ACTIONS.toLowerCase().indexOf("show other users");
	assert.notEqual(stubAt, -1, 'must mention showing other users\' pins somewhere');
	const around = VISIBILITY_ACTIONS.slice(Math.max(0, stubAt - 300), stubAt + 300);
	assert.match(around, /rb-inert/i);
	assert.match(around, /not implemented/i);
	assert.match(VISIBILITY_ACTIONS, /community feature/i);
});
