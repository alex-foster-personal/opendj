/**
 * The Set notes store: the pad (SETNOTE-6340), the note on it (SETNOTE-6340c) and
 * every HTTP call the feature makes, in one module so the feature adds one
 * importer of the API client (the quality ratchet's fan-in gate).
 *
 * The pad's own state - open, scope, geometry, dark paper - lives in localStorage so a
 * reload restores it before any request and an unsaved draft keeps its place.
 * The note lives in state.db behind GET/PUT /api/v1/set-notes/{scope}. A
 * scope with no saved note loads as an empty draft at rev 0 and is only
 * created by the first real edit, so opening the pad writes nothing.
 */
import { api, ApiError, getTrack, unwrap, type Track } from '$lib/api';
import type { components } from '$lib/api-types';

export type { Track };

export type SetNoteWire = components['schemas']['SetNoteOut'];
export type SetNotePut = components['schemas']['SetNotePutIn'];
export type TrackHit = { stable_id: string; title: string | null; artist: string | null };
export type PlaylistScope = { playlist_id: string; name: string };

export const SCRATCH_SCOPE = 'scratch';
export const MIN_PAD_W = 220;
export const MIN_PAD_H = 160;
const SAVE_DEBOUNCE_MS = 600;
const UI_STORAGE_KEY = 'opendj.set-notes.ui';

export interface PadUi {
	open: boolean;
	scope: string;
	x: number;
	y: number;
	w: number;
	h: number;
	dark: boolean;
}
type Geometry = Partial<Pick<PadUi, 'x' | 'y' | 'w' | 'h'>>;

const DEFAULT_UI: PadUi = { open: false, scope: SCRATCH_SCOPE, x: 96, y: 96, w: 340, h: 380, dark: false };

// ----- pad state (localStorage) ---------------------------------------------

function _storage(): Storage | null {
	try {
		return typeof window === 'undefined' ? null : window.localStorage;
	} catch (error) {
		console.error('Set notes: localStorage is unavailable', error);
		return null;
	}
}

function _num(v: unknown, d: number): number {
	return typeof v === 'number' && Number.isFinite(v) ? v : d;
}

function _readUi(): PadUi {
	try {
		const parsed = JSON.parse(_storage()?.getItem(UI_STORAGE_KEY) ?? '{}') as Partial<PadUi>;
		return {
			open: parsed.open === true,
			scope: typeof parsed.scope === 'string' && parsed.scope ? parsed.scope : SCRATCH_SCOPE,
			x: _num(parsed.x, DEFAULT_UI.x),
			y: _num(parsed.y, DEFAULT_UI.y),
			w: _num(parsed.w, DEFAULT_UI.w),
			h: _num(parsed.h, DEFAULT_UI.h),
			dark: parsed.dark === true
		};
	} catch (error) {
		console.error('Set notes: saved pad state is unreadable, using defaults', error);
		return { ...DEFAULT_UI };
	}
}

export const padUi: PadUi = $state(_readUi());

function _persistUi(): void {
	_storage()?.setItem(UI_STORAGE_KEY, JSON.stringify(padUi));
}

export function setSetNotesOpen(open: boolean): void {
	if (padUi.open === open) return;
	padUi.open = open;
	_persistUi();
}

export function toggleSetNotes(): void {
	setSetNotesOpen(!padUi.open);
}

export function toggleSetNotesDark(): void {
	padUi.dark = !padUi.dark;
	_persistUi();
}

// ----- HTTP -------------------------------------------------------------------

function _isStatus(error: unknown, status: number): error is ApiError {
	return error instanceof ApiError && error.status === status;
}

/** The saved note, or null when the scope has none yet (404). */
async function _getNote(scope: string): Promise<SetNoteWire | null> {
	try {
		return await unwrap(api.GET('/api/v1/set-notes/{scope}', { params: { path: { scope } } }));
	} catch (error) {
		if (_isStatus(error, 404)) return null;
		throw error;
	}
}

function _putNote(scope: string, body: SetNotePut): Promise<SetNoteWire> {
	return unwrap(api.PUT('/api/v1/set-notes/{scope}', { params: { path: { scope } }, body }));
}

/** On a 409 stale_rev, the rev the server holds now; otherwise undefined. */
function _staleRevOf(error: unknown): number | null | undefined {
	if (!_isStatus(error, 409)) return undefined;
	const detail = (error.body as { detail?: { current_rev?: unknown } } | null)?.detail;
	return typeof detail?.current_rev === 'number' ? detail.current_rev : null;
}

export async function searchTracks(q: string, limit: number): Promise<TrackHit[]> {
	const data = await unwrap(api.GET('/api/v1/search', { params: { query: { q, limit } } }));
	return data.items;
}

export function listPlaylistScopes(): Promise<PlaylistScope[]> {
	return unwrap(api.GET('/api/v1/playlists'));
}

const _tracks = new Map<string, Promise<Track | null>>();

/** The library's track, or null on a real 404; any other failure throws and
 * is not remembered. Once per id per session, through getTrack, which routes
 * a USB stick id to the stick's own endpoint. */
export function lookupTrack(stableId: string): Promise<Track | null> {
	let found = _tracks.get(stableId);
	if (found === undefined) {
		found = getTrack(stableId).then(
			({ track }) => track,
			(error: unknown) => {
				if (_isStatus(error, 404)) return null;
				_tracks.delete(stableId);
				throw error;
			}
		);
		_tracks.set(stableId, found);
	}
	return found;
}

export async function trackExists(stableId: string): Promise<boolean> {
	return (await lookupTrack(stableId)) !== null;
}

// ----- note sync (debounced, serialized saves) ---------------------------------

export const setNote = $state({
	body: '',
	rev: 0,
	updatedAt: null as string | null,
	loadedScope: null as string | null,
	loading: false,
	saving: false,
	/** Edited since the last successful save (the debounce or a failed save). */
	dirty: false,
	error: null as string | null
});

let _saveTimer: ReturnType<typeof setTimeout> | null = null;
let _inFlight: Promise<void> | null = null;

function _message(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

function _applyNote(data: SetNoteWire | null): void {
	setNote.body = data?.body_md ?? '';
	setNote.rev = data?.rev ?? 0;
	setNote.updatedAt = data?.updated_at ?? null;
	if (!data) return;
	for (const k of ['x', 'y', 'w', 'h'] as const) if (data[k] !== null) padUi[k] = data[k];
}

async function _fetchNote(scope: string): Promise<void> {
	padUi.scope = scope;
	_persistUi();
	setNote.loading = true;
	setNote.error = null;
	let loaded = false;
	try {
		const data = await _getNote(scope);
		if (padUi.scope === scope) _applyNote(data);
		loaded = true;
	} catch (error) {
		if (padUi.scope !== scope) return;
		// Never an empty draft: the scope stays unloaded, so nothing can save over the real note.
		setNote.loadedScope = null;
		setNote.error = `Could not load note: ${_message(error)}`;
	} finally {
		if (padUi.scope === scope) {
			setNote.loading = false;
			if (loaded) setNote.loadedScope = scope;
		}
	}
}

async function _save(): Promise<void> {
	if (!setNote.dirty || setNote.loadedScope !== padUi.scope) return;
	setNote.dirty = false;
	// An untouched empty draft is not a note: moving the pad must not create one.
	if (setNote.rev === 0 && setNote.body === '') return;
	const scope = padUi.scope;
	setNote.saving = true;
	try {
		const data = await _putNote(scope, {
			body_md: setNote.body,
			x: Math.round(padUi.x),
			y: Math.round(padUi.y),
			w: Math.round(padUi.w),
			h: Math.round(padUi.h),
			visible: padUi.open,
			base_rev: setNote.rev
		});
		if (padUi.scope === scope) {
			setNote.rev = data.rev;
			setNote.updatedAt = data.updated_at;
			setNote.error = null;
		}
	} catch (error) {
		const current = _staleRevOf(error);
		if (current !== undefined) {
			// Neither side is lost: the draft stays dirty, and joins the other writer's text under its rev.
			setNote.dirty = true;
			if (padUi.scope !== scope) return;
			try {
				const server = await _getNote(scope);
				if (padUi.scope !== scope) return;
				const draft = setNote.body;
				_applyNote(server);
				if (server && server.body_md !== draft) setNote.body = `${server.body_md}\n\n${draft}`;
				else setNote.body = draft;
				setNote.error = `This note was changed elsewhere (now rev ${current ?? '?'}). Both versions are kept: yours is below theirs.`;
				_markDirty();
			} catch (fetchError) {
				setNote.error = `This note was changed elsewhere, and reading it failed: ${_message(fetchError)}. Your text is kept here, unsaved.`;
			}
			return;
		}
		// Left dirty, not retried on a timer: the next edit or flush tries again.
		setNote.dirty = true;
		setNote.error = `Could not save note: ${_message(error)}`;
	} finally {
		setNote.saving = false;
	}
}

function _runSave(): Promise<void> {
	if (_inFlight === null) {
		_inFlight = _save().finally(() => {
			_inFlight = null;
		});
		return _inFlight;
	}
	// A save already in flight carries the old rev; queue behind it.
	return _inFlight.then(() => _runSave());
}

function _markDirty(): void {
	setNote.dirty = true;
	if (_saveTimer !== null) clearTimeout(_saveTimer);
	_saveTimer = setTimeout(() => {
		_saveTimer = null;
		void _runSave();
	}, SAVE_DEBOUNCE_MS);
}

/** Save now instead of waiting out the debounce (scope switch, unmount). */
export async function flushSetNote(): Promise<void> {
	if (_saveTimer !== null) {
		clearTimeout(_saveTimer);
		_saveTimer = null;
	}
	if (setNote.dirty || _inFlight !== null) await _runSave();
}

/** Switch to a scope; refuses (false) while the current draft could not be saved, so it is never replaced. */
export async function loadSetNote(scope: string): Promise<boolean> {
	await flushSetNote();
	if (setNote.dirty && setNote.loadedScope !== null) {
		setNote.error ??= 'Could not save this note, so it stays open here.';
		return false;
	}
	await _fetchNote(scope);
	return true;
}

/** The pad's mount: load the remembered scope unless it is already loaded. */
export function ensureSetNoteLoaded(): void {
	if (setNote.loadedScope !== padUi.scope) void loadSetNote(padUi.scope);
}

export function editSetNote(body: string): void {
	setNote.body = body;
	_markDirty();
}

export function moveSetNotesPad(geometry: Geometry): void {
	Object.assign(padUi, geometry);
	_persistUi();
	_markDirty();
}
