/**
 * localStorage-backed UI prefs for the /performance browser (FR-1).
 *
 * Rune module - the .svelte.ts extension is REQUIRED for $state
 * (RECON-FRONTEND 10.1). One JSON blob under STORAGE_KEY.
 *
 * Fail-fast policy: a MISSING key is the real first-run state and yields
 * the documented defaults; a PRESENT but malformed blob throws loudly
 * (no silent reset - clear the key to recover). The app is SPA-only
 * (ssr=false in +layout.ts) so localStorage always exists in the browser;
 * the typeof guard only protects unit tests.
 */

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';

/** Library track-table row density (browser list only - not decks/mixer). */
export type LibraryDensity = 'compact' | 'cosy';

/** App + /performance chrome theme. Default dark. */
export type UiTheme = 'dark' | 'light';

/** Preferred vendor writeback targets (preference only; CLI writeback today). */
export type AutoSyncDestination = 'rekordbox' | 'djay' | 'open_dj';

export interface AutoSyncPrefs {
	rekordbox: boolean;
	djay: boolean;
	open_dj: boolean;
}

export interface RbUiPrefs {
	/** FR-1: when true, missing-file tracks are hidden from every pane's
	 * track list AND playlists with available_count == 0 are hidden from
	 * the tree. Default OFF (broken rows render grayed-out but visible). */
	hide_broken_links: boolean;
	/** Track-table row height: compact = current tight rows; cosy = taller. */
	library_density: LibraryDensity;
	/** When true, every transport relocate (including master) uses BAR
	 * phase-preserving sync so bar 1 stays aligned across synced decks. */
	beat_sync_max: boolean;
	/** Library list: keep only tracks appropriate as next (Camelot + BPM
	 * window vs master / loaded reference). Toggle with Tab. */
	next_only_filter: boolean;
	/**
	 * Auto-play next track onto a free/stopped follower when the playing
	 * source enters the remaining-time window (~16s). Hard-cut v1.
	 */
	auto_play_enabled: boolean;
	/**
	 * When true, AutoPlay walks strict playlist order after the current track.
	 * When false (default), picks earliest un-played membership row with
	 * Camelot key +-1 and BPM inside Beat Sync pitch bounds.
	 */
	auto_play_enforce_order: boolean;
	/**
	 * Smart AutoPlay only: prefer the compatible next track with the fewest
	 * onward options (slack / maximize reachable chain). Off = greedy earliest.
	 */
	auto_play_maximize_reach: boolean;
	/** Light/dark chrome. Default dark. Applied to documentElement. */
	theme: UiTheme;
	/** Hide grayed PARITY-TODO rows in the settings overlay. */
	hide_todo_settings: boolean;
	/**
	 * Preferred auto-sync destinations. Persisted for setup/settings;
	 * vendor DB writeback is still manual CLI (apps/sync/apply_ratings.py).
	 */
	auto_sync: AutoSyncPrefs;
	/**
	 * Destructive / move confirms: false = skip the prompt forever.
	 * Missing keys mean "ask". Persisted under the same blob.
	 */
	confirm: {
		delete_playlist?: boolean;
		playlist_drop_mode?: 'add' | 'move';
		/** false = skip double-click Load+play confirm (do this every time). */
		dblclick_load_play?: boolean;
	};
}

const DEFAULTS: RbUiPrefs = {
	hide_broken_links: false,
	library_density: 'compact',
	beat_sync_max: true,
	next_only_filter: false,
	auto_play_enabled: true,
	auto_play_enforce_order: false,
	auto_play_maximize_reach: true,
	theme: 'dark',
	hide_todo_settings: false,
	auto_sync: { rekordbox: false, djay: false, open_dj: false },
	confirm: {}
};

// ----------------------------------------------------------- _helpers

function _storage(): Storage | null {
	return typeof window === 'undefined' ? null : window.localStorage;
}

function _applyThemeDom(theme: UiTheme): void {
	if (typeof document === 'undefined') return;
	document.documentElement.dataset.theme = theme;
	document.documentElement.style.colorScheme = theme;
}

function _load(): RbUiPrefs {
	const storage = _storage();
	if (storage === null) return { ...DEFAULTS };
	const raw = storage.getItem(STORAGE_KEY);
	if (raw === null) return { ...DEFAULTS }; // first run - the one real default
	const parsed = JSON.parse(raw) as Partial<RbUiPrefs>; // malformed JSON throws - intended
	if (typeof parsed.hide_broken_links !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (hide_broken_links is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	const density = parsed.library_density;
	if (density !== undefined && density !== 'compact' && density !== 'cosy') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (library_density must be 'compact'|'cosy') - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.beat_sync_max !== undefined && typeof parsed.beat_sync_max !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (beat_sync_max is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.next_only_filter !== undefined && typeof parsed.next_only_filter !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (next_only_filter is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.auto_play_enabled !== undefined && typeof parsed.auto_play_enabled !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (auto_play_enabled is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (
		parsed.auto_play_enforce_order !== undefined &&
		typeof parsed.auto_play_enforce_order !== 'boolean'
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (auto_play_enforce_order is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (
		parsed.auto_play_maximize_reach !== undefined &&
		typeof parsed.auto_play_maximize_reach !== 'boolean'
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (auto_play_maximize_reach is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	const theme = parsed.theme;
	if (theme !== undefined && theme !== 'dark' && theme !== 'light') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (theme must be 'dark'|'light') - ` +
				'clear the localStorage key to recover'
		);
	}
	if (parsed.hide_todo_settings !== undefined && typeof parsed.hide_todo_settings !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (hide_todo_settings is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	const autoSync = _parseAutoSync(parsed.auto_sync);
	const confirm = parsed.confirm ?? DEFAULTS.confirm;
	if (confirm !== null && typeof confirm !== 'object') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (confirm must be an object) - ` +
				'clear the localStorage key to recover'
		);
	}
	const dropMode = (confirm as RbUiPrefs['confirm']).playlist_drop_mode;
	if (dropMode !== undefined && dropMode !== 'add' && dropMode !== 'move') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (confirm.playlist_drop_mode must be 'add'|'move') - ` +
				'clear the localStorage key to recover'
		);
	}
	return {
		hide_broken_links: parsed.hide_broken_links,
		library_density: density ?? DEFAULTS.library_density,
		beat_sync_max: parsed.beat_sync_max ?? DEFAULTS.beat_sync_max,
		next_only_filter: parsed.next_only_filter ?? DEFAULTS.next_only_filter,
		auto_play_enabled: parsed.auto_play_enabled ?? DEFAULTS.auto_play_enabled,
		auto_play_enforce_order:
			parsed.auto_play_enforce_order ?? DEFAULTS.auto_play_enforce_order,
		auto_play_maximize_reach:
			parsed.auto_play_maximize_reach ?? DEFAULTS.auto_play_maximize_reach,
		theme: theme ?? DEFAULTS.theme,
		hide_todo_settings: parsed.hide_todo_settings ?? DEFAULTS.hide_todo_settings,
		auto_sync: autoSync,
		confirm: { ...(confirm as RbUiPrefs['confirm']) }
	};
}

function _parseAutoSync(raw: unknown): AutoSyncPrefs {
	if (raw === undefined) return { ...DEFAULTS.auto_sync };
	if (raw === null || typeof raw !== 'object') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (auto_sync must be an object) - ` +
				'clear the localStorage key to recover'
		);
	}
	const obj = raw as Partial<AutoSyncPrefs>;
	for (const key of ['rekordbox', 'djay', 'open_dj'] as const) {
		if (obj[key] !== undefined && typeof obj[key] !== 'boolean') {
			throw new Error(
				`${STORAGE_KEY}: malformed prefs blob (auto_sync.${key} is not a boolean) - ` +
					'clear the localStorage key to recover'
			);
		}
	}
	return {
		rekordbox: obj.rekordbox ?? DEFAULTS.auto_sync.rekordbox,
		djay: obj.djay ?? DEFAULTS.auto_sync.djay,
		open_dj: obj.open_dj ?? DEFAULTS.auto_sync.open_dj
	};
}

function _persist(): void {
	_storage()?.setItem(STORAGE_KEY, JSON.stringify($state.snapshot(uiPrefs)));
}

type DiskPrefsPatch = {
	confirm?: RbUiPrefs['confirm'];
	theme?: UiTheme;
	hide_todo_settings?: boolean;
	auto_sync?: AutoSyncPrefs;
};

async function _syncDiskPrefs(patch: DiskPrefsPatch): Promise<void> {
	try {
		await fetch('/api/v1/ui-prefs', {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
			body: JSON.stringify(patch)
		});
	} catch {
		/* localStorage remains authoritative if daemon is down */
	}
}

// -------------------------------------------------------- public API

/** Reactive prefs singleton. Read anywhere; write ONLY via the setters
 * below so every change persists. */
export const uiPrefs = $state<RbUiPrefs>(_load());

_applyThemeDom(uiPrefs.theme);

export function setHideBrokenLinks(next: boolean): void {
	uiPrefs.hide_broken_links = next;
	_persist();
}

export function setLibraryDensity(next: LibraryDensity): void {
	uiPrefs.library_density = next;
	_persist();
}

export function setBeatSyncMax(next: boolean): void {
	uiPrefs.beat_sync_max = next;
	_persist();
}

export function setAutoPlayEnabled(next: boolean): void {
	uiPrefs.auto_play_enabled = next;
	_persist();
}

export function setAutoPlayEnforceOrder(next: boolean): void {
	uiPrefs.auto_play_enforce_order = next;
	_persist();
}

export function setAutoPlayMaximizeReach(next: boolean): void {
	uiPrefs.auto_play_maximize_reach = next;
	_persist();
}

export function setNextOnlyFilter(next: boolean): void {
	uiPrefs.next_only_filter = next;
	_persist();
}

export function toggleNextOnlyFilter(): void {
	setNextOnlyFilter(!uiPrefs.next_only_filter);
}

export function setTheme(next: UiTheme): void {
	uiPrefs.theme = next;
	_applyThemeDom(next);
	_persist();
	void _syncDiskPrefs({ theme: next });
}

export function toggleTheme(): void {
	setTheme(uiPrefs.theme === 'dark' ? 'light' : 'dark');
}

export function setHideTodoSettings(next: boolean): void {
	uiPrefs.hide_todo_settings = next;
	_persist();
	void _syncDiskPrefs({ hide_todo_settings: next });
}

export function setAutoSyncDestination(dest: AutoSyncDestination, next: boolean): void {
	uiPrefs.auto_sync[dest] = next;
	_persist();
	void _syncDiskPrefs({ auto_sync: { ...uiPrefs.auto_sync } });
}

export function setAutoSync(next: AutoSyncPrefs): void {
	uiPrefs.auto_sync = { ...next };
	_persist();
	void _syncDiskPrefs({ auto_sync: { ...uiPrefs.auto_sync } });
}

/** Persist a confirm skip / remembered choice. Pass `undefined` to clear. */
export function setConfirmPref<K extends keyof RbUiPrefs['confirm']>(
	key: K,
	value: RbUiPrefs['confirm'][K] | undefined
): void {
	if (value === undefined) {
		delete uiPrefs.confirm[key];
	} else {
		uiPrefs.confirm[key] = value;
	}
	_persist();
	void _syncDiskPrefs({ confirm: uiPrefs.confirm });
}

/** Pull on-disk confirm + theme prefs once (daemon may have remembered choices). */
export async function hydrateConfirmPrefsFromDisk(): Promise<void> {
	try {
		const r = await fetch('/api/v1/ui-prefs', { headers: { Accept: 'application/json' } });
		if (!r.ok) return;
		const body = (await r.json()) as {
			confirm?: RbUiPrefs['confirm'];
			theme?: UiTheme;
			hide_todo_settings?: boolean;
			auto_sync?: AutoSyncPrefs;
		};
		if (body.confirm !== undefined) {
			uiPrefs.confirm = { ...uiPrefs.confirm, ...body.confirm };
		}
		if (body.theme === 'dark' || body.theme === 'light') {
			uiPrefs.theme = body.theme;
			_applyThemeDom(body.theme);
		}
		if (typeof body.hide_todo_settings === 'boolean') {
			uiPrefs.hide_todo_settings = body.hide_todo_settings;
		}
		if (body.auto_sync !== undefined && typeof body.auto_sync === 'object') {
			uiPrefs.auto_sync = _parseAutoSync(body.auto_sync);
		}
		_persist();
	} catch {
		/* ignore */
	}
}
