// Extra library window at /second-screen (issue #10300). Pure helpers live
// here so node:test can cover monitor pick, the window registry and saved
// geometry without launching Electron.

export const SECOND_SCREEN_WINDOW_LABEL = 'second-screen';
export const SECOND_SCREEN_MENU_LABEL = 'Library on second screen';
export const SECOND_SCREEN_ACCELERATOR = 'CmdOrCtrl+Shift+L';
export const SECOND_SCREEN_ROUTE = '/second-screen';
export const WINDOW_LABELS_STATE_FILE = 'window-labels.json';

export type WindowAction = 'open' | 'close' | 'focus';

export interface Rect {
	x: number;
	y: number;
	width: number;
	height: number;
}

export interface DisplayLike {
	id: number;
	bounds: Rect;
	workArea: Rect;
}

export type SavedGeometry = Rect;

export interface SecondScreenWindow {
	focus(): void;
	close(): void;
	isDestroyed(): boolean;
}

export interface SecondScreenDeps {
	origin(): string | null;
	mainBounds(): Rect;
	displays(): { items: readonly DisplayLike[]; primaryId: number };
	createWindow(opts: { bounds: Rect; url: string; onClosed: () => void }): SecondScreenWindow;
	loadGeometry(label: string): SavedGeometry | null;
	saveGeometry(label: string, geometry: SavedGeometry): void;
}

export function secondScreenUrl(origin: string): string {
	return `${origin.replace(/\/$/, '')}${SECOND_SCREEN_ROUTE}`;
}

export function unknownWindowLabelMessage(label: string): string {
	return `unknown window label ${JSON.stringify(label)}; known: ${SECOND_SCREEN_WINDOW_LABEL}`;
}

export function parseWindowIpcArgs(action: unknown, label: unknown): { action: WindowAction; label: typeof SECOND_SCREEN_WINDOW_LABEL } {
	if (action !== 'open' && action !== 'close' && action !== 'focus') {
		throw new Error(`window action must be open, close or focus, got ${String(action)}`);
	}
	if (label !== SECOND_SCREEN_WINDOW_LABEL) {
		throw new Error(unknownWindowLabelMessage(String(label)));
	}
	return { action, label };
}

/** The display that does not contain the main window's centre, or the primary. */
export function pickSecondScreenDisplay(
	displays: readonly DisplayLike[],
	mainBounds: Rect,
	primaryId: number
): DisplayLike {
	if (displays.length === 0) {
		throw new Error('no displays are available for the second screen');
	}
	const centerX = mainBounds.x + mainBounds.width / 2;
	const centerY = mainBounds.y + mainBounds.height / 2;
	const containsCenter = (display: DisplayLike): boolean => {
		const box = display.bounds;
		return centerX >= box.x && centerX < box.x + box.width && centerY >= box.y && centerY < box.y + box.height;
	};
	const other = displays.find((display) => !containsCenter(display));
	if (other !== undefined) return other;
	const primary = displays.find((display) => display.id === primaryId);
	if (primary !== undefined) return primary;
	const first = displays[0];
	if (first === undefined) throw new Error('no displays are available for the second screen');
	return first;
}

export function placeSecondScreen(saved: SavedGeometry | null, workArea: Rect): Rect {
	if (saved === null) return { x: workArea.x, y: workArea.y, width: workArea.width, height: workArea.height };
	return { x: saved.x, y: saved.y, width: saved.width, height: saved.height };
}

export function parseWindowLabelsState(raw: string): Record<string, SavedGeometry> {
	if (raw.trim() === '') return {};
	const document = JSON.parse(raw) as unknown;
	if (document === null || typeof document !== 'object' || Array.isArray(document)) return {};
	const labels = (document as { labels?: unknown }).labels;
	if (labels === null || typeof labels !== 'object' || Array.isArray(labels)) return {};
	const out: Record<string, SavedGeometry> = {};
	for (const [label, value] of Object.entries(labels)) {
		if (value === null || typeof value !== 'object') continue;
		const row = value as Partial<SavedGeometry>;
		if (
			typeof row.x !== 'number' ||
			typeof row.y !== 'number' ||
			typeof row.width !== 'number' ||
			typeof row.height !== 'number'
		) {
			continue;
		}
		out[label] = {
			x: row.x,
			y: row.y,
			width: row.width,
			height: row.height
		};
	}
	return out;
}

export function serializeWindowLabelsState(labels: Record<string, SavedGeometry>): string {
	return `${JSON.stringify({ version: 1, labels }, null, 2)}\n`;
}

export class SecondScreenController {
	private current: SecondScreenWindow | null = null;

	constructor(private readonly deps: SecondScreenDeps) {}

	control(action: WindowAction, label: string): void {
		const parsed = parseWindowIpcArgs(action, label);
		if (parsed.action === 'close') {
			this.close();
			return;
		}
		this.open();
		if (parsed.action === 'focus') this.current?.focus();
	}

	open(): void {
		if (this.current !== null && !this.current.isDestroyed()) {
			this.current.focus();
			return;
		}
		const origin = this.deps.origin();
		if (origin === null || origin === '') {
			throw new Error('the engine origin is not ready; the second screen cannot open yet');
		}
		const { items, primaryId } = this.deps.displays();
		const picked = pickSecondScreenDisplay(items, this.deps.mainBounds(), primaryId);
		const saved = this.deps.loadGeometry(SECOND_SCREEN_WINDOW_LABEL);
		const bounds = placeSecondScreen(saved, picked.workArea);
		this.current = this.deps.createWindow({
			bounds,
			url: secondScreenUrl(origin),
			onClosed: () => {
				this.current = null;
			}
		});
	}

	close(): void {
		const win = this.current;
		if (win === null || win.isDestroyed()) {
			this.current = null;
			return;
		}
		// The window's own 'close' listener saves geometry (one writer), so closing here does not.
		win.close();
	}

	isOpen(): boolean {
		return this.current !== null && !this.current.isDestroyed();
	}
}
