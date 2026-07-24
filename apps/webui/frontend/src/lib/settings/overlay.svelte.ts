/**
 * Global settings overlay open/close + search history.
 * Mounted from root layout so Cmd+, works on /performance and the shell.
 */

export interface SettingsHistoryEntry {
	query: string;
	group: string | null;
}

let open = $state(false);
let query = $state('');
let group = $state<string | null>(null);
let selectedIndex = $state(0);
let history = $state<SettingsHistoryEntry[]>([]);
let historyIndex = $state(-1);

export function isSettingsOpen(): boolean {
	return open;
}

export function getSettingsQuery(): string {
	return query;
}

export function getSettingsGroup(): string | null {
	return group;
}

export function getSettingsSelectedIndex(): number {
	return selectedIndex;
}

export function canHistoryBack(): boolean {
	return historyIndex > 0;
}

export function canHistoryForward(): boolean {
	return historyIndex >= 0 && historyIndex < history.length - 1;
}

export function openSettings(initialQuery = ''): void {
	open = true;
	query = initialQuery;
	group = null;
	selectedIndex = 0;
	_pushHistory();
}

export function closeSettings(): void {
	open = false;
}

export function toggleSettings(): void {
	if (open) closeSettings();
	else openSettings();
}

export function setSettingsQuery(next: string): void {
	query = next;
	selectedIndex = 0;
}

export function setSettingsGroup(next: string | null): void {
	group = next;
	selectedIndex = 0;
	_pushHistory();
}

export function setSettingsSelectedIndex(next: number): void {
	selectedIndex = next;
}

export function commitSettingsSearch(): void {
	_pushHistory();
}

export function historyBack(): void {
	if (!canHistoryBack()) return;
	historyIndex -= 1;
	_applyHistory();
}

export function historyForward(): void {
	if (!canHistoryForward()) return;
	historyIndex += 1;
	_applyHistory();
}

function _pushHistory(): void {
	const entry: SettingsHistoryEntry = { query, group };
	const truncated = history.slice(0, historyIndex + 1);
	const last = truncated[truncated.length - 1];
	if (last && last.query === entry.query && last.group === entry.group) {
		history = truncated;
		historyIndex = truncated.length - 1;
		return;
	}
	history = [...truncated, entry];
	historyIndex = history.length - 1;
}

function _applyHistory(): void {
	const entry = history[historyIndex];
	if (!entry) return;
	query = entry.query;
	group = entry.group;
	selectedIndex = 0;
}

/** Reactive snapshot for Svelte components (read via $derived in overlay). */
export const settingsOverlay = {
	get open() {
		return open;
	},
	get query() {
		return query;
	},
	get group() {
		return group;
	},
	get selectedIndex() {
		return selectedIndex;
	}
};
