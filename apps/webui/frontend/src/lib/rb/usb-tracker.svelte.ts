/**
 * #328 USB stick tracker - client store of known/seen volumes.
 *
 * Backend lists present mounts via GET /api/v1/usb/volumes (read-only).
 * Local metadata (yours / music / name / forgotten) persists in localStorage.
 * Never writes to a USB mount from this pass.
 */
import { ApiError, api, unwrap } from '../api/client';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import { pushToast } from '$lib/stores.svelte';

const STORAGE_KEY = 'mdt.rb.usb-volumes.v1';
const POLL_MS = 5000;
/** Ceiling of the failure backoff: a refusal that will not clear by itself
 * (diskutil missing, a build that may not look) costs one request a minute,
 * not twelve. */
const POLL_BACKOFF_MAX_MS = 60000;

export type UsbKind = 'rekordbox' | 'djay' | 'music' | 'unknown';
export type UsbRole = 'usb_stick' | 'mounted_drive' | 'disk_image' | 'other';
/** Can Open DJ read the volume (USBPLAY-02)? 'pending' = the macOS
 * Removable Volumes prompt is still open; 'denied' = the user refused it. */
export type UsbAccess = 'ok' | 'pending' | 'denied' | 'unknown';

export interface UsbVolumeKnown {
	id: string;
	name: string;
	mount_path?: string;
	/** The verdict the list folds on. Recomputed from the daemon on every
	 * poll (see _musicVerdict), so an automatic verdict never outlives it. */
	is_music?: boolean;
	/** The user's own answer to "Music stick?" in the first-seen dialog;
	 * absent until answered. Only a "Not music" answer outranks the daemon. */
	music_answer?: boolean;
	forgotten?: boolean;
	/** Epoch ms when user clicked forget (for reason tag). */
	forgotten_at?: number | null;
	/** User said "yours" in first-seen modal (null = unanswered). */
	yours?: boolean | null;
	first_seen: number;
	last_seen: number;
	kind?: UsbKind;
	role?: UsbRole;
	protocol?: string | null;
	/** Server/user reason for fold, e.g. not-usb(mounted drive - USB). */
	hide_reason?: string | null;
	present?: boolean;
	simulated?: boolean;
	/** Needs first-seen modal (yours / music / name / forget). */
	needs_prompt?: boolean;
	/** Absent until the daemon reports it (older daemons never do). */
	access?: UsbAccess;
}

type ApiVolume = {
	id: string;
	name: string;
	mount_path?: string | null;
	kind: UsbKind;
	present: boolean;
	simulated: boolean;
	is_music: boolean;
	role?: UsbRole;
	protocol?: string | null;
	hide_reason?: string | null;
	access?: UsbAccess;
};

type ApiList = { volumes: ApiVolume[]; scanned_at: number };

/** Where volume discovery stands. 'unknown' until the first poll answers;
 * 'unavailable' is the daemon's own 503 refusal (it names a reason);
 * 'unreachable' is everything else that stopped an answer arriving. */
export type UsbDiscoveryState = 'unknown' | 'ok' | 'unavailable' | 'unreachable';

/** Outcome of one stick's library read. The volume list can already say
 * "rekordbox" while export.pdb itself fails, which is the RBX-but-unreadable
 * case the USB modal has to spell out. */
export type UsbLibraryReadNote = {
	state: 'ok' | 'failed';
	code?: string;
	detail?: string;
	trackCount?: number;
};

// ----- state -------------------------------------------------------------

export const usbTracker = $state({
	volumes: _load(),
	panelOpen: false,
	promptId: null as string | null,
	polling: false,
	lastError: null as string | null,
	discovery: 'unknown' as UsbDiscoveryState,
	/** The daemon's `detail.reason` on a 503 refusal, e.g. diskutil_unavailable. */
	unavailableReason: null as string | null,
	/** The daemon's own sentence for the refusal, when it sent one. */
	unavailableTitle: null as string | null,
	/** Polls failed in a row; drives the backoff, 0 after any success. */
	consecutiveFailures: 0,
	/** Daemon `scanned_at` of the last successful poll this session; null
	 * until one lands, while every `present` flag is still last session's. */
	scannedAt: null as number | null,
	/** Library GET outcome by volume id. Not persisted: a new session re-reads. */
	readNotes: {} as Record<string, UsbLibraryReadNote>,
	/** Which refresh the listing came from (see `usbRefreshSeq`); 0 before any. */
	listingSeq: 0
});

let _pollTimer: ReturnType<typeof setTimeout> | null = null;
let _started = false;
/** Bumped by every start and stop, so a poll in flight across a stop (or a
 * stop then start) cannot schedule a second chain. */
let _watchGeneration = 0;

// ----- persistence -------------------------------------------------------

function _storage(): Storage | null {
	return typeof window === 'undefined' ? null : window.localStorage;
}

function _load(): UsbVolumeKnown[] {
	const storage = _storage();
	if (storage === null) return [];
	const raw = storage.getItem(STORAGE_KEY);
	if (raw === null) return [];
	try {
		const parsed = JSON.parse(raw) as unknown;
		if (!Array.isArray(parsed)) return [];
		return parsed.filter(_isKnown).map((v) => ({ ...v }));
	} catch {
		return [];
	}
}

function _isKnown(v: unknown): v is UsbVolumeKnown {
	if (v === null || typeof v !== 'object') return false;
	const o = v as Record<string, unknown>;
	return (
		typeof o.id === 'string' &&
		typeof o.name === 'string' &&
		typeof o.first_seen === 'number' &&
		typeof o.last_seen === 'number'
	);
}

function _persist(): void {
	_storage()?.setItem(STORAGE_KEY, JSON.stringify($state.snapshot(usbTracker.volumes)));
}

// ----- pure helpers (unit-tested) ----------------------------------------

/** Open DJ cannot read the volume's root yet (USBPLAY-02): 'pending' is an
 * open macOS prompt (or a drive still spinning up), 'denied' a refusal. The
 * daemon answers a blocked USB stick as role usb_stick, is_music true, so it
 * stays listed with its state; a blocked Fixed drive or disk image keeps its
 * own role and folds away as before. */
export function usbAccessBlocked(v: { access?: UsbAccess | undefined }): boolean {
	return v.access === 'pending' || v.access === 'denied';
}

/** Active ribbon/list: present music sticks that are not forgotten. */
export function isActiveUsbRow(v: UsbVolumeKnown): boolean {
	return Boolean(v.present) && !v.forgotten && v.is_music !== false;
}

/** Folded Non-music / Forgotten section. */
export function isFoldedUsbRow(v: UsbVolumeKnown): boolean {
	return Boolean(v.forgotten) || v.is_music === false;
}

export function presentNonForgotten(): UsbVolumeKnown[] {
	return usbTracker.volumes.filter(isActiveUsbRow);
}

export function foldedAway(): UsbVolumeKnown[] {
	return usbTracker.volumes.filter(isFoldedUsbRow);
}

/** Ordinal day suffix: 1st, 2nd, 3rd, 4th... */
export function dayOrdinal(day: number): string {
	const n = Math.trunc(day);
	const mod100 = n % 100;
	if (mod100 >= 11 && mod100 <= 13) return 'th';
	switch (n % 10) {
		case 1:
			return 'st';
		case 2:
			return 'nd';
		case 3:
			return 'rd';
		default:
			return 'th';
	}
}

/** e.g. 8th Aug '26 */
export function formatShortDate(ms: number): string {
	const d = new Date(ms);
	if (!Number.isFinite(d.getTime())) return 'unknown date';
	const day = d.getDate();
	const mon = d.toLocaleString('en-GB', { month: 'short' });
	const yr = String(d.getFullYear()).slice(-2);
	return `${day}${dayOrdinal(day)} ${mon} '${yr}`;
}

export function usbRowReasonTag(v: UsbVolumeKnown): string | null {
	if (v.forgotten) {
		const when =
			typeof v.forgotten_at === 'number' && Number.isFinite(v.forgotten_at)
				? formatShortDate(v.forgotten_at)
				: null;
		return when ? `user clicked forget ${when}` : 'user clicked forget';
	}
	if (v.hide_reason) return v.hide_reason;
	if (v.is_music === false) {
		if (v.role === 'mounted_drive') return 'not-usb(mounted drive)';
		if (v.role === 'disk_image') return 'not-usb(disk image)';
		return 'non-music';
	}
	return null;
}

export function usbRowKindLabel(v: UsbVolumeKnown): string {
	if (v.role === 'mounted_drive') return 'mounted drive · non-music';
	if (v.role === 'disk_image') return 'disk image · non-music';
	if (v.is_music === false) return 'non-music';
	const kind = v.kind ?? 'unknown';
	return v.simulated ? `${kind} (simulated)` : kind;
}

export type UsbFormatVolume = Pick<UsbVolumeKnown, 'id' | 'name'> &
	Partial<Pick<UsbVolumeKnown, 'present' | 'kind' | 'role' | 'is_music' | 'forgotten'>>;

export type UsbFormatStickLine = {
	id: string;
	name: string;
	version: string;
};

export type UsbFormatReport = {
	/** Whether the machine has a single present volume or more than one. */
	drives: string;
	/** Whether the music sticks are only rekordbox (RBX), only the other
	 * library, or both. */
	libraries: string;
	sticks: UsbFormatStickLine[];
};

function _libraryKindWords(kind: UsbKind | undefined): string {
	switch (kind) {
		case 'rekordbox':
			return 'rekordbox (RBX)';
		case 'djay':
			return 'djay';
		case 'music':
			return 'music files';
		default:
			return 'unrecognized';
	}
}

function _versionSentence(
	kind: UsbKind | undefined,
	note: UsbLibraryReadNote | undefined
): string {
	if (note?.state === 'ok') {
		const tracks =
			typeof note.trackCount === 'number' ? `${note.trackCount} tracks. ` : '';
		if (kind === 'rekordbox') {
			return (
				`${tracks}Version found: rekordbox Device Library (export.pdb, RBX). ` +
				'The pdb file version number is not reported. ' +
				'Device Library Plus, the other rekordbox export, was not part of this read.'
			);
		}
		return `${tracks}Version found: ${_libraryKindWords(kind)}. Not a rekordbox (RBX) export.`;
	}
	if (note?.state === 'failed') {
		const why = note.detail ? ` (${note.detail})` : '';
		if (kind === 'rekordbox') {
			return (
				`USB read failed${why}, but this drive is rekordbox (RBX) format. ` +
				'Version number was not read. This app only opens export.pdb (Device Library). ' +
				'A stick that is only Device Library Plus, the other export, fails this read.'
			);
		}
		return `USB read failed${why}. Classified as ${_libraryKindWords(kind)}, not rekordbox (RBX).`;
	}
	if (kind === 'rekordbox') {
		return (
			'Classified as rekordbox (RBX). Version not read yet: open the stick to read export.pdb. ' +
			'Whether it is only Device Library or only Device Library Plus is not known until that read.'
		);
	}
	if (kind === 'djay') {
		return 'Classified as djay, the other library. Not rekordbox (RBX).';
	}
	return `No library version identified (${_libraryKindWords(kind)}).`;
}

/** What the USB modal says about the drives plugged in right now.
 * Assumption: "version" is the library class (RBX export.pdb vs djay vs
 * Device Library Plus). This build's volume list has no pdb version number,
 * so a failed RBX read is reported as RBX with the version unread. */
export function usbFormatReport(
	volumes: readonly UsbFormatVolume[],
	reads: Readonly<Record<string, UsbLibraryReadNote>>,
	scanned: boolean
): UsbFormatReport {
	// Before this session's first scan every `present` flag is last session's,
	// so naming a drive as mounted could name one already unplugged.
	if (!scanned) {
		return {
			drives: 'Checking drives: none scanned yet this session.',
			libraries: 'Not known until the first scan.',
			sticks: []
		};
	}
	const present = volumes.filter((v) => v.present === true);
	const names = present.map((v) => v.name || v.id);
	const drives =
		present.length === 0
			? 'No drive found.'
			: present.length === 1
				? `One drive only: ${names[0]}.`
				: `Not one drive only. ${present.length} drives: ${names.join(', ')}.`;
	const typed = present.filter((v) => v.kind === 'rekordbox' || v.kind === 'djay');
	const kinds = new Set(typed.map((v) => v.kind));
	const libraries =
		typed.length === 0
			? 'Not classified as rekordbox (RBX) or the other library (djay).'
			: kinds.size > 1
				? 'Both rekordbox (RBX) and djay are mounted. Not only one or the other.'
				: kinds.has('rekordbox')
					? 'Only rekordbox (RBX). Not the other library (djay).'
					: 'Only djay, the other library. Not rekordbox (RBX).';
	return {
		drives,
		libraries,
		sticks: present.map((v) => ({
			id: v.id,
			name: v.name || v.id,
			version: _versionSentence(v.kind, reads[v.id])
		}))
	};
}

export function noteUsbLibraryRead(volumeId: string, note: UsbLibraryReadNote): void {
	usbTracker.readNotes = { ...usbTracker.readNotes, [volumeId]: note };
}

/** Tells the user about a USB action that did not happen (an eject that never
 * started), as an error toast. Raised from here, where the USB toasts already
 * are, so USB modules do not each import the toast store. */
export function announceUsbProblem(message: string): void {
	pushToast(message, 'error');
}

export function openUsbPanel(id?: string): void {
	usbTracker.panelOpen = true;
	if (id !== undefined) {
		const vol = usbTracker.volumes.find((v) => v.id === id);
		if (vol?.needs_prompt) {
			usbTracker.promptId = id;
		}
	}
}

export function closeUsbPanel(): void {
	usbTracker.panelOpen = false;
}

export function toggleUsbPanel(): void {
	usbTracker.panelOpen = !usbTracker.panelOpen;
}

// ----- mutations ---------------------------------------------------------

export function applyFirstSeen(
	id: string,
	patch: {
		yours?: boolean;
		is_music?: boolean;
		name?: string;
		forgotten?: boolean;
	}
): void {
	const row = usbTracker.volumes.find((v) => v.id === id);
	if (!row) return;
	if (patch.yours !== undefined) row.yours = patch.yours;
	if (patch.is_music !== undefined) {
		row.music_answer = patch.is_music;
		row.is_music = patch.is_music;
	}
	if (patch.name !== undefined && patch.name.trim() !== '') row.name = patch.name.trim();
	if (patch.forgotten !== undefined) {
		row.forgotten = patch.forgotten;
		row.forgotten_at = patch.forgotten ? Date.now() : null;
	}
	row.needs_prompt = false;
	if (usbTracker.promptId === id) usbTracker.promptId = null;
	_persist();
}

export function setForgotten(id: string, forgotten: boolean): void {
	const row = usbTracker.volumes.find((v) => v.id === id);
	if (!row) return;
	row.forgotten = forgotten;
	row.forgotten_at = forgotten ? Date.now() : null;
	if (forgotten) row.needs_prompt = false;
	_persist();
}

// ----- polling / API -----------------------------------------------------

/** Wait before the next poll: POLL_MS while healthy, doubling per failure in
 * a row up to POLL_BACKOFF_MAX_MS. One success returns it to POLL_MS. */
export function usbPollDelayMs(consecutiveFailures: number): number {
	if (consecutiveFailures <= 0) return POLL_MS;
	return Math.min(POLL_MS * 2 ** consecutiveFailures, POLL_BACKOFF_MAX_MS);
}

const _REASON_SENTENCES: Record<string, string> = {
	diskutil_unavailable: 'macOS diskutil was not found at /usr/sbin/diskutil',
	diskutil_timed_out: 'macOS diskutil did not answer in time',
	diskutil_query_failed: 'macOS diskutil returned an error',
	volumes_root_unavailable: '/Volumes is not available on this machine',
	volumes_root_unreadable: '/Volumes could not be listed',
	volumes_changing: 'USB sticks kept changing while /Volumes was read; it will be read again'
};

/** The sentence the USB panel shows instead of an empty list it cannot
 * vouch for. null when discovery is working or has not answered yet. */
export function usbDiscoveryNotice(t: {
	discovery: UsbDiscoveryState;
	unavailableReason: string | null;
	unavailableTitle: string | null;
	lastError: string | null;
}): string | null {
	if (t.discovery === 'unavailable') {
		const reason = t.unavailableReason ?? 'no reason given';
		const why = t.unavailableTitle ?? _REASON_SENTENCES[reason] ?? reason;
		return `USB discovery unavailable: ${why}`;
	} else if (t.discovery === 'unreachable') {
		return `USB discovery unreachable: ${t.lastError ?? 'no answer from the engine'}`;
	}
	return null;
}

function _scheduleNextPoll(generation: number): void {
	_pollTimer = setTimeout(() => {
		_pollTimer = null;
		void _pollOnce(generation);
	}, usbPollDelayMs(usbTracker.consecutiveFailures));
}

async function _pollOnce(generation: number): Promise<void> {
	await refreshUsbVolumes();
	if (_started && generation === _watchGeneration) _scheduleNextPoll(generation);
}

export function startUsbWatch(): void {
	if (_started) return;
	_started = true;
	_watchGeneration += 1;
	usbTracker.polling = true;
	void _pollOnce(_watchGeneration);
}

export function stopUsbWatch(): void {
	_started = false;
	_watchGeneration += 1;
	usbTracker.polling = false;
	if (_pollTimer !== null) {
		clearTimeout(_pollTimer);
		_pollTimer = null;
	}
}

/** `detail.reason` / `detail.ui_title` of the daemon's 503 refusal body. */
function _refusalDetail(body: unknown): { reason: string | null; title: string | null } {
	const detail = (body as { detail?: unknown } | null)?.detail;
	if (detail === null || typeof detail !== 'object') return { reason: null, title: null };
	const { reason, ui_title } = detail as { reason?: unknown; ui_title?: unknown };
	return {
		reason: typeof reason === 'string' ? reason : null,
		title: typeof ui_title === 'string' ? ui_title : null
	};
}

/**
 * Sequence of the latest volume read started. Reads overlap (the poll, the
 * export screen opening, the rescan after an eject), and only the newest one
 * may write the tracker: an older answer landing late would put an ejected
 * stick back on the list as present (Codex P2 4288381672).
 */
let _refreshSeq = 0;

/** The newest refresh started so far. A listing with a higher `listingSeq`
 * was requested after this moment; an older request in flight is dropped
 * once a newer one starts, so it never lands after it. */
export function usbRefreshSeq(): number {
	return _refreshSeq;
}

/**
 * What is wrong with a volume listing the daemon answered 200 with, or null.
 * A listing missing `volumes` is contract drift, not "no sticks": read as
 * empty it would mark every stick gone and drop the export screen's picked
 * stick and plan (Sol P1 4350275754), so it is a discovery error instead.
 */
/** UsbVolumeOut's fields every daemon sends, by type. `_ingest` skips a row
 * whose `present` is not true and then marks every remembered stick absent, so
 * a row missing one of these would read as an unplug rather than an error. */
const _ROW_REQUIRED: Record<string, 'string' | 'boolean'> = {
	id: 'string',
	name: 'string',
	present: 'boolean',
	simulated: 'boolean',
	is_music: 'boolean'
};
/** UsbVolumeOut's Literal fields, by allowed value. A Record over each union
 * makes the compiler refuse a missing or extra value. A value outside its set
 * (a typo such as "usb-stick") would replace the stored role and read as the
 * stick being unplugged, so it is malformed too (Codex 4351111102). */
const _KINDS: Record<UsbKind, true> = { rekordbox: true, djay: true, music: true, unknown: true };
const _ROLES: Record<UsbRole, true> = { usb_stick: true, mounted_drive: true, disk_image: true, other: true };
const _ACCESS: Record<UsbAccess, true> = { ok: true, pending: true, denied: true, unknown: true };
const _ROW_ENUMS: Record<string, { values: Record<string, true>; optional: boolean }> = {
	kind: { values: _KINDS, optional: false },
	role: { values: _ROLES, optional: false },
	// Older daemons never send `access` (USBPLAY-02).
	access: { values: _ACCESS, optional: true }
};
/** Fields an older daemon may leave out; when sent they must be a string or null. */
const _ROW_OPTIONAL_STRINGS = ['mount_path', 'protocol', 'hide_reason'] as const;

function _malformedRowFields(row: unknown): string[] {
	if (row === null || typeof row !== 'object' || Array.isArray(row)) return ['volumes[]'];
	const r = row as Record<string, unknown>;
	const wrong = Object.entries(_ROW_REQUIRED)
		.filter(([key, type]) => typeof r[key] !== type)
		.map(([key]) => `volumes[].${key}`);
	for (const [key, { values, optional }] of Object.entries(_ROW_ENUMS)) {
		const value = r[key];
		if (optional && value === undefined) continue;
		if (typeof value !== 'string' || !Object.hasOwn(values, value)) wrong.push(`volumes[].${key}`);
	}
	for (const key of _ROW_OPTIONAL_STRINGS) {
		const value = r[key];
		if (value === undefined || value === null || typeof value === 'string') continue;
		wrong.push(`volumes[].${key}`);
	}
	return wrong;
}

function _malformedListing(body: unknown): string | null {
	const { volumes, scanned_at } = (body ?? {}) as { volumes?: unknown; scanned_at?: unknown };
	const wrong: string[] = [];
	if (!Array.isArray(volumes)) wrong.push('volumes');
	else wrong.push(...new Set(volumes.flatMap(_malformedRowFields)));
	if (typeof scanned_at !== 'number' || !Number.isFinite(scanned_at)) wrong.push('scanned_at');
	return wrong.length === 0 ? null : wrong.join(', ');
}

export async function refreshUsbVolumes(): Promise<void> {
	const seq = ++_refreshSeq;
	try {
		const body = await unwrap(api.GET('/api/v1/usb/volumes'));
		if (seq !== _refreshSeq) return;
		const malformed = _malformedListing(body);
		if (malformed !== null) throw new Error(`the daemon's USB volume answer is malformed: ${malformed}`);
		_ingest(body.volumes);
		usbTracker.scannedAt = body.scanned_at;
		usbTracker.listingSeq = seq;
		usbTracker.lastError = null;
		usbTracker.discovery = 'ok';
		usbTracker.unavailableReason = null;
		usbTracker.unavailableTitle = null;
		usbTracker.consecutiveFailures = 0;
	} catch (exc) {
		if (seq !== _refreshSeq) return;
		usbTracker.consecutiveFailures += 1;
		if (exc instanceof ApiError) {
			usbTracker.lastError = `usb volumes HTTP ${exc.status}`;
			const refusal = _refusalDetail(exc.body);
			if (exc.status === 503 && refusal.reason !== null) {
				usbTracker.discovery = 'unavailable';
				usbTracker.unavailableReason = refusal.reason;
				usbTracker.unavailableTitle = refusal.title;
				return;
			}
		} else {
			usbTracker.lastError = exc instanceof Error ? exc.message : String(exc);
		}
		usbTracker.discovery = 'unreachable';
		usbTracker.unavailableReason = null;
		usbTracker.unavailableTitle = null;
	}
}

/**
 * The music verdict to store for a volume on this poll. The user's "Not
 * music" answer wins. Otherwise it is the daemon's current verdict, with Fixed
 * drives and disk images never music. Recomputed on every poll, so a verdict
 * the tracker stored automatically never outlives the daemon's opinion: a
 * stick an older daemon reported as a mounted drive while permission was
 * refused, or one whose root did not list in time, is re-judged once it reads.
 */
function _musicVerdict(api: ApiVolume, role: UsbRole, answer: boolean | undefined): boolean {
	if (answer === false) return false;
	return role !== 'mounted_drive' && role !== 'disk_image' && api.is_music;
}

function _ingest(remote: ApiVolume[]): void {
	const now = Date.now();
	const byId = new Map(usbTracker.volumes.map((v) => [v.id, v]));
	_mergeAliasIds(byId, remote);
	const seen = new Set<string>();
	const freshIds: string[] = [];

	for (const api of remote) {
		if (!api.present) continue;
		seen.add(api.id);
		const prev = byId.get(api.id);
		const role = api.role ?? 'other';
		const autoNonMusic = !_musicVerdict(api, role, undefined);
		if (prev === undefined) {
			const row: UsbVolumeKnown = {
				id: api.id,
				name: api.name,
				...(api.mount_path == null ? {} : { mount_path: api.mount_path }),
				is_music: !autoNonMusic,
				...(api.access === undefined ? {} : { access: api.access }),
				forgotten: false,
				forgotten_at: null,
				yours: null,
				first_seen: now,
				last_seen: now,
				kind: api.kind,
				role,
				protocol: api.protocol ?? null,
				hide_reason: api.hide_reason ?? null,
				present: true,
				simulated: api.simulated,
				// Skip first-seen nag for Fixed HDDs / disk images.
				needs_prompt: !autoNonMusic
			};
			byId.set(api.id, row);
			freshIds.push(api.id);
		} else {
			prev.last_seen = now;
			prev.present = true;
			// The newest healthy row decides the mount too: one with no mount no
			// longer establishes where the stick is, so Check plan and Eject are not
			// offered at an older path (Sol P1 4398465000).
			if (api.mount_path == null) delete prev.mount_path;
			else prev.mount_path = api.mount_path;
			prev.kind = api.kind;
			prev.role = role;
			// The newest healthy row decides the bus: one that omits it no longer
			// establishes USB, so Check plan and Eject are not offered on a stale
			// value (Sol P1 on 091295d344, Codex 4350696755, Sol 4352227073).
			prev.protocol = api.protocol ?? null;
			prev.simulated = api.simulated;
			if (api.hide_reason) prev.hide_reason = api.hide_reason;
			if (api.access !== undefined) prev.access = api.access;
			if (autoNonMusic) prev.needs_prompt = false;
			prev.is_music = _musicVerdict(api, role, prev.music_answer);
			// Keep user rename; only fill empty.
			if (!prev.name) prev.name = api.name;
		}
	}

	const gone: string[] = [];
	for (const row of byId.values()) {
		if (seen.has(row.id)) continue;
		row.present = false;
		gone.push(row.id);
	}
	// A removed stick's read note describes a read of the stick as it was, so
	// a replug shows nothing until it is read again (Sol P2, PR #6223).
	if (gone.some((id) => id in usbTracker.readNotes)) {
		const notes = { ...usbTracker.readNotes };
		for (const id of gone) delete notes[id];
		usbTracker.readNotes = notes;
	}

	usbTracker.volumes = [...byId.values()].sort((a, b) => b.last_seen - a.last_seen);
	_persist();

	for (const id of freshIds) {
		const row = byId.get(id);
		if (!row || row.forgotten || !row.needs_prompt) continue;
		_onNewDetect(row);
	}
}

/** Prefer vol:UUID over path:Name when the same mount remounts. */
function _mergeAliasIds(byId: Map<string, UsbVolumeKnown>, remote: ApiVolume[]): void {
	for (const api of remote) {
		if (!api.id.startsWith('vol:')) continue;
		const mount = api.mount_path ?? null;
		const pathAlias = mount
			? [...byId.values()].find(
					(v) =>
						v.id.startsWith('path:') &&
						(v.mount_path === mount || v.name === api.name)
				)
			: [...byId.values()].find((v) => v.id === `path:${api.name}`);
		if (!pathAlias || pathAlias.id === api.id) continue;
		if (byId.has(api.id)) {
			byId.delete(pathAlias.id);
			continue;
		}
		byId.delete(pathAlias.id);
		pathAlias.id = api.id;
		byId.set(api.id, pathAlias);
	}
}

function _onNewDetect(row: UsbVolumeKnown): void {
	if (uiPrefs.usb_toast_enabled) {
		pushToast(`USB detected: ${row.name}`, 'info', uiPrefs.usb_toast_ms);
	}
	if (uiPrefs.usb_auto_open_panel) {
		usbTracker.panelOpen = true;
	}
	if (row.needs_prompt) {
		usbTracker.promptId = row.id;
		usbTracker.panelOpen = true;
	}
}
