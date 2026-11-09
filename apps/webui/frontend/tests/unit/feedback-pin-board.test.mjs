/**
 * Pin board badges and draw rules (issue #3782, FB-15).
 */

import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let board;
before(async () => {
	board = await loadTypeScriptModule('src/lib/rb/feedback-pin-board.ts');
});

const pin = (over = {}) => ({
	id: 'abc123',
	x_pct: 50,
	y_pct: 50,
	anchor: '#deck-a',
	page: '/performance',
	text: 'test',
	created_at: '2026-09-01T09:00:00Z',
	status: 'open',
	environment: { viewport_width: 1000, viewport_height: 800 },
	...over
});

test('isPinDrawn hides only archived pins', () => {
	assert.equal(board.isPinDrawn(pin()), true);
	assert.equal(board.isPinDrawn(pin({ status: 'harvested' })), true);
	assert.equal(board.isPinDrawn(pin({ status: 'archived' })), false);
});

test('route-moved badge when page differs from pathname', () => {
	const state = board.pinBoardState(pin({ page: '/library' }), '/performance', false);
	assert.ok(state.badges.includes('route-moved'));
});

test('anchorMovedAtPin compares describeAnchor at pixel point', () => {
	const moved = board.anchorMovedAtPin(pin({ anchor: '#old' }), () => ({
		id: 'new',
		tagName: 'BUTTON'
	}));
	assert.equal(moved, true);
	const same = board.anchorMovedAtPin(pin({ anchor: '#deck-a' }), () => ({
		id: 'deck-a',
		tagName: 'DIV'
	}));
	assert.equal(same, false);
});

test('anchorMovedAtPin never fires for a pin without a pixel position', () => {
	let probed = 0;
	const moved = board.anchorMovedAtPin(
		pin({ anchor: '#old', x_pct: undefined, y_pct: undefined }),
		() => {
			probed += 1;
			return { id: 'new', tagName: 'BUTTON' };
		}
	);
	assert.equal(moved, false);
	assert.equal(probed, 0);
});

test('regressed badge when updated_at is after fixed_at', () => {
	const state = board.pinBoardState(
		pin({
			status: 'open',
			fixed_in_sha: 'abc1234',
			fixed_at: '2026-09-01T10:00:00.000000Z',
			updated_at: '2026-09-02T10:00:00.000000Z'
		}),
		'/performance',
		false
	);
	assert.ok(state.badges.includes('regressed'));
});

test('fixed and harvested pins are not regressed after silent update', () => {
	for (const status of ['fixed', 'harvested']) {
		const state = board.pinBoardState(
			pin({
				status,
				fixed_in_sha: 'abc1234',
				fixed_at: '2026-09-01T10:00:00.000000Z',
				updated_at: '2026-09-02T10:00:00.000000Z'
			}),
			'/performance',
			false
		);
		assert.ok(!state.badges.includes('regressed'));
	}
});

function el(tag, classes = '', { id, parent, ariaLabel } = {}) {
	const classNames = classes ? classes.split(/\s+/).filter(Boolean) : [];
	const node = {
		tagName: tag.toUpperCase(),
		id: id ?? '',
		classList: {
			[Symbol.iterator]() {
				return classNames.values();
			},
			has(name) {
				return classNames.includes(name);
			},
			get length() {
				return classNames.length;
			},
			item(i) {
				return classNames[i] ?? null;
			}
		},
		parentElement: parent ?? null,
		getAttribute(name) {
			if (name === 'aria-label') return ariaLabel ?? null;
			return null;
		},
		closest(selector) {
			const wanted = selector.split(',').map((s) => s.trim().replace(/^\./, ''));
			let cur = this;
			while (cur) {
				for (const cls of wanted) {
					if (cur.classList?.has(cls)) return cur;
				}
				cur = cur.parentElement;
			}
			return null;
		}
	};
	return node;
}

function markerButton() {
	return el('button', 'fb-pin', { ariaLabel: 'Comment pin (default) - open' });
}

function target(id) {
	return el('div', '', { id });
}

test('anchorMovedBeneathPin ignores the marker button in the stack', () => {
	const deck = target('deck-a');
	const marker = markerButton();
	const moved = board.anchorMovedBeneathPin(pin({ anchor: '#deck-a' }), () => [marker, deck]);
	assert.equal(moved, false);
});

test('anchorMovedBeneathPin ignores a child inside pin chrome', () => {
	const deck = target('deck-a');
	const marker = markerButton();
	const child = el('span', 'fb-pin-badge', { parent: marker });
	const moved = board.anchorMovedBeneathPin(pin({ anchor: '#deck-a' }), () => [
		child,
		marker,
		deck
	]);
	assert.equal(moved, false);
});

test('anchorMovedBeneathPin reports drift when target element changed', () => {
	const marker = markerButton();
	const other = target('new-element');
	const moved = board.anchorMovedBeneathPin(pin({ anchor: '#deck-a' }), () => [marker, other]);
	assert.equal(moved, true);
});
