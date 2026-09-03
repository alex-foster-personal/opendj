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

import { api, unwrap } from '../api/client';

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';

/** Playlist tree width bounds in CSS pixels. Keep enough room for hierarchy
 * labels while preserving a useful track pane on compact displays. */
export const PLAYLIST_TREE_WIDTH_MIN = 220;
export const PLAYLIST_TREE_WIDTH_MAX = 520;
export const PLAYLIST_TREE_WIDTH_DEFAULT = 300;

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
	/** Width, in CSS pixels, of the resizable playlist tree (220 through 520). */
	playlist_tree_width: number;
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
	/** #328 USB tracker: toast when a new stick is detected. */
	usb_toast_enabled: boolean;
	/** Dismiss delay for that toast, in milliseconds. */
	usb_toast_ms: number;
	/**
	 * Open the USB panel on ANY new detect. Off by default: a volume that
	 * still needs its first-seen answers already forces the panel open, so
	 * this only adds the intrusion for sticks that need nothing.
	 */
	usb_auto_open_panel: boolean;
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
	/**
	 * Identity of the playlist the first browser pane last held, restored on
	 * the next boot of /performance so the track table does not open blank.
	 * null = nothing remembered yet (first run), which resolves to All Tracks
	 * when the library is non-empty. Identity only - counts are re-fetched,
	 * never restored, so a stale number can never reach the screen.
	 */
	last_playlist: LastPlaylistPref | null;
}

/** Persisted pane identity. Mirrors BootPlaylistChoice in the pane contract,
 * declared here so prefs owns its own storage shape rather than importing a
 * component module into the prefs layer. */
export interface LastPlaylistPref {
	playlist_id: string;
	name: string;
	kind: 'all_tracks' | 'playlist';
}

const DEFAULTS: RbUiPrefs = {
	playlist_tree_width: PLAYLIST_TREE_WIDTH_DEFAULT,
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
	usb_toast_enabled: true,
	usb_toast_ms: 5000,
	usb_auto_open_panel: false,
	confirm: {},
	last_playlist: null
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
	const treeWidth = parsed.playlist_tree_width;
	if (
		treeWidth !== undefined &&
		(typeof treeWidth !== 'number' ||
			!Number.isFinite(treeWidth) ||
			!Number.isInteger(treeWidth) ||
			treeWidth < PLAYLIST_TREE_WIDTH_MIN ||
			treeWidth > PLAYLIST_TREE_WIDTH_MAX)
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (playlist_tree_width must be an integer from ` +
				`${PLAYLIST_TREE_WIDTH_MIN} through ${PLAYLIST_TREE_WIDTH_MAX}) - clear the localStorage key to recover`
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
	if (parsed.usb_toast_enabled !== undefined && typeof parsed.usb_toast_enabled !== 'boolean') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (usb_toast_enabled is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (
		parsed.usb_toast_ms !== undefined &&
		(typeof parsed.usb_toast_ms !== 'number' ||
			!Number.isFinite(parsed.usb_toast_ms) ||
			parsed.usb_toast_ms <= 0)
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (usb_toast_ms must be a positive finite number) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (
		parsed.usb_auto_open_panel !== undefined &&
		typeof parsed.usb_auto_open_panel !== 'boolean'
	) {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (usb_auto_open_panel is not a boolean) - ` +
				'clear the localStorage key to recover'
		);
	}
	const lastPlaylist = _parseLastPlaylist(parsed.last_playlist);
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
		playlist_tree_width: treeWidth ?? DEFAULTS.playlist_tree_width,
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
		usb_toast_enabled: parsed.usb_toast_enabled ?? DEFAULTS.usb_toast_enabled,
		usb_toast_ms: parsed.usb_toast_ms ?? DEFAULTS.usb_toast_ms,
		usb_auto_open_panel: parsed.usb_auto_open_panel ?? DEFAULTS.usb_auto_open_panel,
		confirm: { ...(confirm as RbUiPrefs['confirm']) },
		last_playlist: lastPlaylist
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

/** Absent (old blob written before this field existed) is the real first-run
 * state and yields null; present but the wrong shape throws, same as every
 * other field here - a half-valid pane identity would restore into a load
 * against an id that is not a string. */
function _parseLastPlaylist(raw: unknown): LastPlaylistPref | null {
	if (raw === undefined || raw === null) return null;
	if (typeof raw !== 'object') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (last_playlist must be an object or null) - ` +
				'clear the localStorage key to recover'
		);
	}
	const obj = raw as Partial<LastPlaylistPref>;
	if (typeof obj.playlist_id !== 'string' || obj.playlist_id === '') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (last_playlist.playlist_id must be a non-empty string) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (typeof obj.name !== 'string') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (last_playlist.name must be a string) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (obj.kind !== 'all_tracks' && obj.kind !== 'playlist') {
		throw new Error(
			`${STORAGE_KEY}: malformed prefs blob (last_playlist.kind must be 'all_tracks'|'playlist') - ` +
				'clear the localStorage key to recover'
		);
	}
	return { playlist_id: obj.playlist_id, name: obj.name, kind: obj.kind };
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
		await api.PUT('/api/v1/ui-prefs', { body: patch });
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

/** Persist the tree width after clamping it to its documented 220-520px range. */
export function setPlaylistTreeWidth(next: number): void {
	if (!Number.isFinite(next)) {
		throw new Error('playlist tree width must be a finite number');
	}
	uiPrefs.playlist_tree_width = Math.round(
		Math.min(PLAYLIST_TREE_WIDTH_MAX, Math.max(PLAYLIST_TREE_WIDTH_MIN, next))
	);
	_persist();
}

/** Remember which playlist the first browser pane holds, so the next boot of
 * /performance restores it instead of opening on a blank track table. Writes
 * only when the identity actually changed - every pane load calls this. */
export function setLastPlaylist(next: LastPlaylistPref | null): void {
	const current = uiPrefs.last_playlist;
	if (next === null) {
		if (current === null) return;
		uiPrefs.last_playlist = null;
		_persist();
		return;
	}
	if (
		current !== null &&
		current.playlist_id === next.playlist_id &&
		current.name === next.name &&
		current.kind === next.kind
	) {
		return;
	}
	uiPrefs.last_playlist = { ...next };
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
		const body = await unwrap(api.GET('/api/v1/ui-prefs')) as {
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
