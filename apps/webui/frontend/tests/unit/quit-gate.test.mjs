/**
 * INSTALL-21 quit confirmation: needsQuitConfirmation and dialog decisions.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let needsQuitConfirmation;
let planQuitRequest;
let isQuitConfirmOpen;
let openQuitConfirm;
let closeQuitConfirm;
let confirmQuit;

function deck(overrides = {}) {
	return {
		stable_id: null,
		playing: false,
		audible: false,
		...overrides
	};
}

function state(deckOverrides = {}) {
	return {
		decks: {
			1: deck(deckOverrides[1] ?? {}),
			2: deck(deckOverrides[2] ?? {}),
			3: deck(deckOverrides[3] ?? {}),
			4: deck(deckOverrides[4] ?? {})
		}
	};
}

before(async () => {
	const needs = await loadTypeScriptModule('src/lib/shell/needs-quit-confirmation.ts');
	needsQuitConfirmation = needs.needsQuitConfirmation;
	const logic = await loadTypeScriptModule('src/lib/shell/quit-gate-logic.ts');
	planQuitRequest = logic.planQuitRequest;
	const overlay = await loadTypeScriptModule('src/lib/shell/quit-gate-state.ts');
	isQuitConfirmOpen = overlay.isQuitConfirmOpen;
	openQuitConfirm = overlay.openQuitConfirm;
	closeQuitConfirm = overlay.closeQuitConfirm;
	const gate = await loadTypeScriptModule('src/lib/shell/quit-gate.ts');
	confirmQuit = gate.confirmQuit;
	closeQuitConfirm();
});

test('needsQuitConfirmation is false only when empty and silent', () => {
	assert.equal(needsQuitConfirmation(state()), false);
});

test('needsQuitConfirmation is true when a deck is loaded but paused', () => {
	assert.equal(needsQuitConfirmation(state({ 1: { stable_id: 'track-001' } })), true);
});

test('needsQuitConfirmation is true when a deck is playing', () => {
	assert.equal(needsQuitConfirmation(state({ 2: { playing: true } })), true);
});

test('needsQuitConfirmation is true when a deck is audible only', () => {
	assert.equal(needsQuitConfirmation(state({ 3: { audible: true } })), true);
});

test('first quit request opens the dialog when confirmation is required', () => {
	closeQuitConfirm();
	assert.equal(
		planQuitRequest({
			dialogOpen: false,
			needsConfirmation: needsQuitConfirmation(state({ 1: { stable_id: 'track-001' } }))
		}),
		'open-dialog'
	);
	openQuitConfirm(null);
	assert.equal(isQuitConfirmOpen(), true);
});

test('second quit request confirms when the dialog is already open', () => {
	assert.equal(
		planQuitRequest({
			dialogOpen: true,
			needsConfirmation: true
		}),
		'confirm'
	);
});

test('force quit skips the dialog', () => {
	assert.equal(
		planQuitRequest({
			force: true,
			dialogOpen: false,
			needsConfirmation: true
		}),
		'confirm'
	);
});

test('empty session quits immediately without opening the dialog', () => {
	assert.equal(
		planQuitRequest({
			dialogOpen: false,
			needsConfirmation: needsQuitConfirmation(state())
		}),
		'confirm'
	);
});

test('cancelQuit closes the dialog', () => {
	openQuitConfirm(null);
	closeQuitConfirm();
	assert.equal(isQuitConfirmOpen(), false);
});

test('confirmQuit flushes before exit', async () => {
	const order = [];
	await confirmQuit({
		flushSnapshot: () => {
			order.push('flush');
		},
		exitShell: async () => {
			order.push('exit');
		}
	});
	assert.deepEqual(order, ['flush', 'exit']);
});
