/**
 * Issue #3990 pins 894af5672c3b and d529a7e80a4e (JIK, /performance `.hp`).
 *
 * 894af5672c3b: hover explainers are categorized (instant vs stay); the I/O
 * hover dismisses instantly and is compact (one sentence, devices in use,
 * "Click I/O to set audio outputs"); MAIN stacks above SPLIT; I/O becomes a
 * double-height SET OUTPUTS button that pulses and glows red while outputs are
 * unset until clicked once this session (reduced motion: glow, no pulse).
 * d529a7e80a4e: the rescan button and its hover explainer live inside the I/O
 * surface, not in the `.hp` row.
 *
 * Preview integration (Fri 2 Oct 2026): main's I/O click menu became the
 * Preview's persistent Audio I/O panel. SET OUTPUTS opens that panel, MAIN /
 * SPLIT and Rescan live inside it, and the hover is the compact summary.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

const CLUSTER = source('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte');
const EXPLAINER = source('../../src/lib/components/rb/deck/ControlExplainer.svelte');

let dismiss;
let io;

before(async () => {
	dismiss = await loadTypeScriptModule('src/lib/rb/explainer-dismiss.ts');
	io = await loadTypeScriptModule('src/lib/rb/io-outputs-button.ts');
});

function ioExplainerTag() {
	const start = CLUSTER.indexOf('title="Audio I/O quick settings"');
	assert.ok(start > 0, 'the Audio I/O explainer must exist');
	const open = CLUSTER.lastIndexOf('<ControlExplainer', start);
	return CLUSTER.slice(open, CLUSTER.indexOf('>', start) + 1);
}

function ioPanel() {
	const start = CLUSTER.indexOf('{#if ioSurface.open}');
	assert.ok(start > 0, 'the Audio I/O panel block must exist');
	const end = CLUSTER.indexOf('{#snippet outputMenu()}', start);
	assert.ok(end > start, 'the panel block ends before the output menu snippet');
	return CLUSTER.slice(start, end);
}

function markupOutsidePanel() {
	const markup = CLUSTER.slice(CLUSTER.indexOf('</script>'), CLUSTER.indexOf('<style>'));
	return markup.replace(ioPanel(), '');
}

// ---- explainer dismiss categories ----

test('explicit dismiss mode wins; omitted resolves by content (action stays, info is instant)', () => {
	assert.equal(dismiss.resolveExplainerDismiss('instant', true), 'instant');
	assert.equal(dismiss.resolveExplainerDismiss('stay', false), 'stay');
	assert.equal(dismiss.resolveExplainerDismiss(undefined, true), 'stay');
	assert.equal(dismiss.resolveExplainerDismiss(undefined, false), 'instant');
});

test('an instant popover is click-through and hides its action until click-pinned', () => {
	assert.equal(dismiss.explainerPopInteractive('instant', false), false);
	assert.equal(dismiss.explainerPopInteractive('instant', true), true);
	assert.equal(dismiss.explainerShowsAction('instant', true, false), false);
	assert.equal(dismiss.explainerShowsAction('instant', true, true), true);
	// opposite direction: a stay popover keeps its action and pointer events on hover
	assert.equal(dismiss.explainerPopInteractive('stay', false), true);
	assert.equal(dismiss.explainerShowsAction('stay', true, false), true);
	assert.equal(dismiss.explainerShowsAction('stay', false, true), false);
});

test('ControlExplainer wires the dismiss mode into the popover', () => {
	assert.match(EXPLAINER, /dismiss\?: ExplainerDismiss;/);
	assert.match(EXPLAINER, /resolveExplainerDismiss\(dismiss, action !== null\)/);
	assert.match(EXPLAINER, /class:click-through=\{!popInteractive\}/);
	assert.match(EXPLAINER, /\.pop\.click-through \{\s*pointer-events: none;\s*\}/);
	assert.match(EXPLAINER, /\{#if showAction && action !== null\}/);
	// the hover grace delay applies only to `stay`; instant closes on pointerleave
	assert.match(EXPLAINER, /event instanceof PointerEvent && dismissMode === 'stay'/);
	assert.doesNotMatch(EXPLAINER, /event instanceof PointerEvent && action !== null/);
});

test('the I/O hover is the compact instant-dismiss summary; the click opens the panel instead of pinning', () => {
	const tag = ioExplainerTag();
	assert.match(tag, /dismiss="instant"/);
	assert.match(tag, /compact=\{true\}/);
	assert.match(tag, /disabled=\{ioSurface\.open\}/);
	assert.match(tag, /bullets=\{ioBullets\}/);
	assert.doesNotMatch(tag, /pinOnClick/);
	assert.match(CLUSTER, /const ioBullets = \$derived\(\[ioSummary\.sentence, \.\.\.ioSummary\.devices, ioSummary\.cta\]\)/);
});

// ---- compact hover content ----

const DEVICES = [
	{ id: 'spk', label: 'MacBook Pro Speakers' },
	{ id: 'hp', label: 'AirPods Pro' }
];

function hp(overrides = {}) {
	return {
		output_mode: 'practice',
		outputs: DEVICES,
		inputs: [{ id: 'mic', label: 'MacBook Pro Microphone' }],
		selected_master_output_device_id: null,
		selected_output_device_id: null,
		selected_input_device_id: null,
		...overrides
	};
}

test('hover summary is one sentence, a compact device list, and the call to action', () => {
	const s = io.ioHoverSummary(
		hp({
			output_mode: 'two_outputs',
			selected_master_output_device_id: 'spk',
			selected_output_device_id: 'hp',
			selected_input_device_id: 'mic'
		})
	);
	assert.equal(s.sentence.split(/[.!?](\s|$)/).filter((x) => x && x.trim()).length, 1, 'exactly one sentence');
	assert.deepEqual(s.devices, ['MAIN: MacBook Pro Speakers', 'CUE: AirPods Pro', 'IN: MacBook Pro Microphone']);
	assert.equal(s.cta, 'Click I/O to set audio outputs');
});

test('hover summary names the routing it knows when no device is chosen', () => {
	assert.deepEqual(io.ioHoverSummary(hp()).devices, ['MAIN: OS default output', 'CUE: blended into MAIN (practice)']);
	assert.deepEqual(io.ioHoverSummary(hp({ output_mode: 'split_cable' })).devices, [
		'MAIN: OS default output',
		'CUE: right leg of MAIN (split cable)'
	]);
	// a pinned id whose label is not enumerated yet is still a choice, not the OS default
	assert.deepEqual(io.ioHoverSummary(hp({ outputs: [], selected_master_output_device_id: 'x' })).devices[0], 'MAIN: chosen device (name not loaded yet)');
});

// ---- SET OUTPUTS alert ----

test('outputs are unset only when neither MASTER nor HEADPHONE CUE is chosen', () => {
	assert.equal(io.outputsUnset(hp()), true);
	assert.equal(io.outputsUnset(hp({ selected_master_output_device_id: 'spk' })), false);
	assert.equal(io.outputsUnset(hp({ selected_output_device_id: 'hp' })), false);
});

test('the alert fires only while unset AND not yet clicked this session', () => {
	assert.equal(io.ioShouldAlert(true, false), true);
	assert.equal(io.ioShouldAlert(true, true), false);
	assert.equal(io.ioShouldAlert(false, false), false);
	assert.equal(io.ioShouldAlert(false, true), false);
});

test('the session click flag round-trips through sessionStorage and survives a throwing store', () => {
	const map = new Map();
	const store = { getItem: (k) => (map.has(k) ? map.get(k) : null), setItem: (k, v) => map.set(k, v) };
	assert.equal(io.readIoClickedThisSession(store), false);
	io.markIoClickedThisSession(store);
	assert.equal(map.get(io.IO_OUTPUTS_CLICKED_SESSION_KEY), '1');
	assert.equal(io.readIoClickedThisSession(store), true);
	const broken = {
		getItem: () => {
			throw new Error('SecurityError');
		},
		setItem: () => {
			throw new Error('QuotaExceeded');
		}
	};
	assert.equal(io.readIoClickedThisSession(broken), false);
	assert.doesNotThrow(() => io.markIoClickedThisSession(broken));
	assert.equal(io.readIoClickedThisSession(null), false);
});

test('SET OUTPUTS is a double-height button bound to the alert, marking the click before opening the panel', () => {
	assert.match(CLUSTER, /aria-label="SHOW AUDIO I\/O"/);
	assert.match(CLUSTER, /class="hp-btn hp-io-trigger hp-btn-io"\s*class:io-alert=\{ioAlert\}/);
	assert.match(CLUSTER, /const ioAlert = \$derived\(ioShouldAlert\(outputsUnset\(headphoneState\), ioOutputsSession\.clicked\)\)/);
	assert.match(CLUSTER, /onclick=\{handleSetOutputs\}><span>SET<\/span><span>OUTPUTS<\/span><\/button/);
	// opening the panel stays side-effect free: no device acquire on this click
	assert.match(CLUSTER, /function handleSetOutputs\(\): void \{\s*noteSetOutputsClicked\(\);\s*openIo\(\);\s*\}/);
	assert.match(CLUSTER, /\.hp-btn-io \{[^}]*flex-direction: column;[^}]*min-height: 23px;/);
	const session = source('../../src/lib/rb/io-outputs-session.svelte.ts');
	assert.match(session, /\$state\(\{ clicked: readIoClickedThisSession\(\) \}\)/);
	assert.match(session, /ioOutputsSession\.clicked = true;\s*markIoClickedThisSession\(\);/);
});

test('the alert glows red and pulses, and reduced motion keeps the glow without the pulse', () => {
	const alert = CLUSTER.match(/\t\.hp-btn-io\.io-alert \{([^}]*)\}/);
	assert.ok(alert, '.hp-btn-io.io-alert rule must exist');
	assert.match(alert[1], /box-shadow: 0 0 6px/);
	assert.match(alert[1], /--rb-red/);
	assert.match(alert[1], /animation: io-alert-pulse/);
	assert.match(CLUSTER, /@keyframes io-alert-pulse/);
	const reduced = CLUSTER.match(/@media \(prefers-reduced-motion: reduce\) \{([\s\S]*?)\n\t\}/);
	assert.ok(reduced, 'reduced-motion block must exist');
	assert.match(reduced[1], /\.hp-btn-io\.io-alert \{\s*animation: none;\s*\}/);
	assert.doesNotMatch(reduced[1], /box-shadow: none/, 'reduced motion keeps the glow');
});

// ---- layout ----

test('MAIN comes before SPLIT in the panel routing choices; practice mode is still offered', () => {
	const panel = ioPanel();
	const start = panel.indexOf('<div class="hp-mode-choices"');
	assert.ok(start > 0, '.hp-mode-choices must sit inside the I/O panel');
	const choices = panel.slice(start, panel.indexOf('</div>', start));
	const main = choices.indexOf('title="MAIN"');
	const split = choices.indexOf('title="SPLIT"');
	assert.ok(main > 0 && split > main, 'MAIN then SPLIT inside .hp-mode-choices');
	assert.match(choices, /onclick=\{\(\) => onmode\('practice'\)\}>MAIN \/ practice</);
});

// ---- pin d529a7e80a4e: rescan explainer lives in the I/O menu ----

test('the rescan button and its explainer are inside the Audio I/O panel', () => {
	assert.match(
		ioPanel(),
		/<ControlExplainer title="Rescan" bullets=\{rescanBullets\}[^>]*>\s*<button[\s\S]*?aria-label="Rescan available headphone output devices"[\s\S]*?onclick=\{onrefresh\}/
	);
});

test('the .hp row outside the panel no longer carries the rescan button or its explainer', () => {
	const outside = markupOutsidePanel();
	assert.ok(outside.includes('class="hp"'), 'control: the slice must contain the .hp row');
	assert.ok(outside.includes('title="Audio I/O quick settings"'), 'control: the slice must contain the I/O explainer');
	assert.doesNotMatch(outside, /title="Rescan"/);
	assert.doesNotMatch(outside, /Rescan available headphone output devices/);
});
