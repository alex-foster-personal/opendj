/**
 * FB-16 criterion 1: pin placement resolves modal elements above main chrome.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let placement;

before(async () => {
	placement = await loadTypeScriptModule('src/lib/rb/feedback-pin-placement.ts');
});

function el(tag, classes = '') {
	const classNames = classes ? classes.split(/\s+/).filter(Boolean) : [];
	return {
		tagName: tag.toUpperCase(),
		classList: {
			[Symbol.iterator]() {
				return classNames.values();
			},
			has(name) {
				return classNames.includes(name);
			}
		},
		parentElement: null,
		closest(selector) {
			let cur = this;
			while (cur) {
				if (selector === '.pop' && cur.classList?.has('pop')) return cur;
				if (selector === '[role="dialog"]' && cur.role === 'dialog') return cur;
				cur = cur.parentElement;
			}
			return null;
		},
		role: undefined
	};
}

test('resolvePinAnchorAt prefers a ControlExplainer pop over main-route chrome', () => {
	const overlay = el('button', 'fb-place-overlay');
	const mainBtn = el('button', 'deck-load');
	const pop = el('div', 'pop');
	const select = el('select', 'hp-device');
	select.parentElement = pop;
	const stack = [overlay, select, pop, mainBtn];
	const hit = placement.resolvePinAnchorAt(100, 200, () => stack);
	assert.equal(hit, select);
});

test('resolvePinAnchorAt skips pin-system overlay and lands on page chrome when no modal', () => {
	const overlay = el('button', 'fb-place-overlay');
	const mainBtn = el('button', 'deck-load');
	const stack = [overlay, mainBtn];
	const hit = placement.resolvePinAnchorAt(50, 50, () => stack);
	assert.equal(hit, mainBtn);
});

test('resolvePinAnchorAt skips pin markers in the stack', () => {
	const overlay = el('button', 'fb-place-overlay');
	const pin = el('button', 'fb-pin');
	const target = el('div', 'waveform');
	const stack = [overlay, pin, target];
	const hit = placement.resolvePinAnchorAt(10, 10, () => stack);
	assert.equal(hit, target);
});

test('resolvePinAnchorAt prefers a root overlay dialog over main-route chrome', () => {
	const overlay = el('button', 'fb-place-overlay');
	const mainBtn = el('button', 'sidebar-link');
	const dialog = el('div', 'so-panel');
	dialog.role = 'dialog';
	const search = el('input', 'so-search');
	search.parentElement = dialog;
	const stack = [overlay, search, dialog, mainBtn];
	const hit = placement.resolvePinAnchorAt(120, 80, () => stack);
	assert.equal(hit, search);
});
