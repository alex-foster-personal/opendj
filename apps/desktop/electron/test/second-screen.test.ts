import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import {
	SECOND_SCREEN_ACCELERATOR,
	SECOND_SCREEN_MENU_LABEL,
	SECOND_SCREEN_ROUTE,
	SECOND_SCREEN_WINDOW_LABEL,
	SecondScreenController,
	type SavedGeometry,
	type SecondScreenWindow,
	parseWindowIpcArgs,
	parseWindowLabelsState,
	pickSecondScreenDisplay,
	placeSecondScreen,
	secondScreenUrl,
	serializeWindowLabelsState,
	unknownWindowLabelMessage
} from '../src/second-screen';

function display(id: number, x: number, y: number, width = 1920, height = 1080) {
	const bounds = { x, y, width, height };
	return { id, bounds, workArea: bounds };
}

test('picks the display that does not contain the main window centre', () => {
	const left = display(1, 0, 0);
	const right = display(2, 1920, 0);
	const picked = pickSecondScreenDisplay([left, right], { x: 100, y: 100, width: 800, height: 600 }, 1);
	assert.equal(picked.id, 2);
});

test('falls back to the primary when only one display is present', () => {
	const only = display(7, 0, 0);
	const picked = pickSecondScreenDisplay([only], { x: 10, y: 10, width: 800, height: 600 }, 7);
	assert.equal(picked.id, 7);
});

test('falls back to the primary when every display contains the centre', () => {
	const primary = display(1, 0, 0, 3840, 2160);
	const nested = display(2, 100, 100, 800, 600);
	const picked = pickSecondScreenDisplay([primary, nested], { x: 120, y: 120, width: 400, height: 300 }, 1);
	assert.equal(picked.id, 1);
});

test('second-screen URL appends the route without a double slash', () => {
	assert.equal(secondScreenUrl('http://127.0.0.1:4723'), `http://127.0.0.1:4723${SECOND_SCREEN_ROUTE}`);
	assert.equal(secondScreenUrl('http://127.0.0.1:4723/'), `http://127.0.0.1:4723${SECOND_SCREEN_ROUTE}`);
});

test('parseWindowIpcArgs accepts open close and focus on the known label', () => {
	assert.deepEqual(parseWindowIpcArgs('open', SECOND_SCREEN_WINDOW_LABEL), {
		action: 'open',
		label: SECOND_SCREEN_WINDOW_LABEL
	});
	assert.deepEqual(parseWindowIpcArgs('focus', 'second-screen'), {
		action: 'focus',
		label: 'second-screen'
	});
});

test('parseWindowIpcArgs refuses an unknown label with the known list', () => {
	assert.throws(() => parseWindowIpcArgs('open', 'mixer'), /known: second-screen/);
	assert.equal(unknownWindowLabelMessage('mixer'), 'unknown window label "mixer"; known: second-screen');
});

test('saved geometry is used when present, otherwise the picked work area', () => {
	const work = { x: 1920, y: 0, width: 1920, height: 1080 };
	assert.deepEqual(placeSecondScreen(null, work), work);
	const saved: SavedGeometry = { x: 2000, y: 40, width: 1200, height: 800 };
	assert.deepEqual(placeSecondScreen(saved, work), { x: 2000, y: 40, width: 1200, height: 800 });
});

test('window-labels.json round-trips geometry keyed by label without touching monitors', () => {
	const raw = serializeWindowLabelsState({
		'second-screen': { x: 10, y: 20, width: 800, height: 600 }
	});
	assert.match(raw, /"version": 1/);
	assert.doesNotMatch(raw, /"monitors"/);
	const parsed = parseWindowLabelsState(raw);
	assert.deepEqual(parsed['second-screen'], { x: 10, y: 20, width: 800, height: 600 });
});

test('parseWindowLabelsState ignores a corrupt document', () => {
	assert.deepEqual(parseWindowLabelsState(''), {});
	assert.deepEqual(parseWindowLabelsState('[]'), {});
	assert.deepEqual(parseWindowLabelsState('{"labels":{"second-screen":{"x":1}}}'), {});
});

class FakeWindow implements SecondScreenWindow {
	closed = false;
	focused = 0;
	bounds: { x: number; y: number; width: number; height: number };
	private readonly onClosed: () => void;

	constructor(bounds: { x: number; y: number; width: number; height: number }, onClosed: () => void) {
		this.bounds = { ...bounds };
		this.onClosed = onClosed;
	}

	focus(): void {
		this.focused += 1;
	}

	close(): void {
		this.closed = true;
		this.onClosed();
	}

	isDestroyed(): boolean {
		return this.closed;
	}
}

function makeController(opts?: { origin?: string | null }) {
	const created: FakeWindow[] = [];
	const saved: Record<string, SavedGeometry> = {};
	const controller = new SecondScreenController({
		origin: () => (opts && 'origin' in opts ? opts.origin ?? null : 'http://127.0.0.1:4723'),
		mainBounds: () => ({ x: 80, y: 80, width: 1280, height: 800 }),
		displays: () => ({
			items: [display(1, 0, 0), display(2, 1920, 0)],
			primaryId: 1
		}),
		createWindow: ({ bounds, url, onClosed }) => {
			assert.equal(url, 'http://127.0.0.1:4723/second-screen');
			assert.equal(bounds.x, 1920);
			const win = new FakeWindow(bounds, onClosed);
			created.push(win);
			return win;
		},
		loadGeometry: (label) => saved[label] ?? null,
		saveGeometry: (label, geometry) => {
			saved[label] = geometry;
		}
	});
	return { controller, created, saved };
}

test('opening twice focuses the existing window instead of creating another', () => {
	const { controller, created } = makeController();
	controller.control('open', 'second-screen');
	controller.control('open', 'second-screen');
	assert.equal(created.length, 1);
	const first = created[0];
	assert.ok(first);
	assert.equal(first.focused, 1);
	assert.equal(controller.isOpen(), true);
});

test('focus when closed opens the window', () => {
	const { controller, created } = makeController();
	controller.control('focus', 'second-screen');
	assert.equal(created.length, 1);
	const opened = created[0];
	assert.ok(opened);
	assert.equal(opened.focused, 1);
});

test('close leaves no registered window and leaves saving to the window close listener', () => {
	const { controller, created, saved } = makeController();
	controller.control('open', 'second-screen');
	controller.control('close', 'second-screen');
	const closed = created[0];
	assert.ok(closed);
	assert.equal(closed.closed, true);
	assert.equal(controller.isOpen(), false);
	assert.equal(saved['second-screen'], undefined);
});

test('close when nothing is open is a no-op', () => {
	const { controller, created } = makeController();
	controller.control('close', 'second-screen');
	assert.equal(created.length, 0);
	assert.equal(controller.isOpen(), false);
});

test('open without an engine origin is refused', () => {
	const { controller } = makeController({ origin: null });
	assert.throws(() => controller.control('open', 'second-screen'), /engine origin is not ready/);
});

test('menu constants match the Tauri Window item and accelerator', () => {
	assert.equal(SECOND_SCREEN_MENU_LABEL, 'Library on second screen');
	assert.equal(SECOND_SCREEN_ACCELERATOR, 'CmdOrCtrl+Shift+L');
});

test('preload exposes the windows IPC used by the page command', () => {
	const source = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'preload.ts'), 'utf8');
	assert.match(source, /opendj:window/);
	assert.match(source, /windows:\s*\{/);
	assert.match(source, /open:\s*\(label/);
	assert.match(source, /close:\s*\(label/);
	assert.match(source, /focus:\s*\(label/);
});

test('main wires the window IPC and menu item to the same controller', () => {
	const source = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'main.ts'), 'utf8');
	assert.match(source, /opendj:window/);
	assert.match(source, /SECOND_SCREEN_MENU_LABEL/);
	assert.match(source, /SECOND_SCREEN_ACCELERATOR/);
	assert.match(source, /SecondScreenController/);
	assert.match(source, /The extra window has no quit gate/);
});
