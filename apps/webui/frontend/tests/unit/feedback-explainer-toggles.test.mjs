/**
 * Pin 88e3abec02a0: the main comment/feedback button gets ONE shared
 * hover/focus explainer (ControlExplainer, the same component already used
 * to teach the /performance controls), worded for an end user, plus two
 * toggles: a working "show feedback comment pins" (default OFF for a new
 * viewer, persisted per-viewer) and a stubbed "show other users' pins"
 * (rb-inert, and a performance-bus command that answers not_implemented).
 *
 * Pin 6af63c5e9b7c: the operator hover breakdown (FB-20) is asserted on the
 * RENDERED controls in feedback-pin-6af63c5e9b7c-rendered-summary.test.mjs.
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
// The markup lives in the props-only view; the container wires the pref in.
const VISIBILITY_ACTIONS = [
	'FeedbackPinVisibilityActions.svelte',
	'FeedbackPinVisibilityActionsView.svelte'
]
	.map((name) => readFileSync(new URL(`../../src/lib/components/rb/${name}`, import.meta.url), 'utf8'))
	.join('\n');
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
	// The switch lives in the shared actions component, so the widget and the
	// shell button both get it without importing prefs themselves.
	assert.match(VISIBILITY_ACTIONS, /showAgentPins=\{uiPrefs\.show_agent_pins\}/);
	assert.match(VISIBILITY_ACTIONS, /checked=\{showAgentPins\}/);
	assert.match(VISIBILITY_ACTIONS, /setShowAgentPins\(!uiPrefs\.show_agent_pins\)/);
	assert.match(VISIBILITY_ACTIONS, /Show agent pins/);
	assert.match(WIDGET, /<FeedbackPinVisibilityActions/);
	// #3790 rewrote the filter as an early return; either spelling is the same
	// gate. The predicate itself moved into PIN_LAYER's pagePins derivation
	// alongside the rest of the pin-visibility gating (FB-16, #3888).
	assert.match(
		PIN_LAYER,
		/p\.author !== 'agent' \|\| uiPrefs\.show_agent_pins|p\.author === 'agent' && !uiPrefs\.show_agent_pins/
	);
});

test('the hover options ask how many days pins stay visible and the layer hides older ones', () => {
	assert.match(VISIBILITY_ACTIONS, /How many days should pins be visible\?/);
	assert.match(VISIBILITY_ACTIONS, /type="number"/);
	assert.match(VISIBILITY_ACTIONS, /setPinVisibleDays/);
	assert.match(PIN_LAYER, /isPinWithinVisibleDays\(p\.created_at, uiPrefs\.pin_visible_days/);
});

/* PGCL-7693g: the days block grew the dock's hover card over the Audio I/O
 * panel, where it took the placement press. comment-pin-io-modal.spec.ts is
 * the behavioral gate; this pins the wiring on both comment buttons. */
test('both comment-pin hover cards close while placement is armed', () => {
	const SHELL = readFileSync(
		new URL('../../src/lib/components/rb/FeedbackPinShellButton.svelte', import.meta.url),
		'utf8'
	);
	for (const [name, src] of [
		['FeedbackWidget', WIDGET],
		['FeedbackPinShellButton', SHELL]
	]) {
		const tag = src.slice(src.indexOf('<ControlExplainer'), src.indexOf('>', src.indexOf('<ControlExplainer')) + 1);
		assert.notEqual(tag, '', `${name} must render a ControlExplainer`);
		assert.match(tag, /disabled=\{feedbackState\.placementArmed\}/, `${name}'s explainer must close while placing`);
	}
	// The prop this relies on must still close an open card, not only block a new one.
	assert.match(EXPLAINER, /if \(disabled\) _close\(\);/);
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
