/**
 * FB-16 criterion 4: selected pin highlights anchored element safely.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let highlight;

before(async () => {
	highlight = await loadTypeScriptModule('src/lib/rb/feedback-pin-anchor-highlight.ts');
});

afterEach(() => {
	highlight.clearPinAnchorHighlight();
	delete globalThis.document;
});

test('applyPinAnchorHighlight toggles fb-pin-anchor-target for id selectors', () => {
	const btn = {
		classList: {
			classes: new Set(),
			add(name) {
				this.classes.add(name);
			},
			remove(name) {
				this.classes.delete(name);
			},
			has(name) {
				return this.classes.has(name);
			}
		}
	};
	globalThis.document = {
		querySelector(sel) {
			return sel === '#io-select' ? btn : null;
		}
	};
	highlight.applyPinAnchorHighlight('#io-select');
	assert.ok(btn.classList.has('fb-pin-anchor-target'));
	highlight.clearPinAnchorHighlight();
	assert.ok(!btn.classList.has('fb-pin-anchor-target'));
});

test('applyPinAnchorHighlight refuses arbitrary selector text', () => {
	const div = {
		classList: {
			classes: new Set(['safe']),
			add(name) {
				this.classes.add(name);
			},
			remove(name) {
				this.classes.delete(name);
			},
			has(name) {
				return this.classes.has(name);
			}
		}
	};
	globalThis.document = {
		querySelector() {
			return div;
		}
	};
	highlight.applyPinAnchorHighlight('button:not(.safe)');
	assert.ok(!div.classList.has('fb-pin-anchor-target'));
});

test('FeedbackPinLayer wires highlight on open pin and clears on close', () => {
	const layer = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/FeedbackPinLayer.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(layer, /applyPinAnchorHighlight/);
	assert.match(layer, /clearPinAnchorHighlight/);
	assert.match(layer, /:global\(\.fb-pin-anchor-target\)/);
});
