/**
 * Play from USB browse store (USBPLAY-05, USBPLAY-09).
 *
 * Owns one cached copy of each opened stick's library, keyed by VolumeUUID,
 * plus the pane side of stick browsing: building a stick pane's rows and
 * keeping them honest when the stick comes and goes.
 *
 * Fetch discipline: GET /api/v1/usb/volumes/{volume_id}/library runs ONLY
 * after a user (or agent) action opens a stick or selects a stick pane,
 * never at mount. On a machine without USB access every /usb route answers
 * 503, and a mount-time fetch would be a console error on every page load
 * (setup-entry-points.spec.ts tolerates exactly one: /usb/volumes).
 *
 * Unplug and replug: when the volume tracker lists a stick as no longer
 * present, its cache is dropped and every pane showing it grays in place
 * ("stick removed", loads refused). When the SAME VolumeUUID is present
 * again, those panes re-read the stick and come back with their rows and
 * selection, because stick track ids are stable per VolumeUUID (USBPLAY-04).
 * The stick tree re-opens from the component side. A stick the tracker has
 * not listed yet, or any stick before the tracker's first poll this session,
 * is UNKNOWN rather than unplugged: an agent can open a stick pane before the
 * first poll lands, and that read must not be killed as a removal.
 *
 * Lazy module: imported by UsbStickTree and by BrowserPanel's stick branch
 * through a dynamic import, so none of it is charged to the /performance
 * first-paint bundle.
 */
import { untrack } from 'svelte';
import { pruneSelection } from '$lib/components/rb/browser/pane-row-selection';
import type { BrowserRow, PaneStore } from '$lib/components/rb/browser/pane-contract.svelte';
import { fetchRbJson, RbApiError } from './api-rb';
import type { PlaylistNode } from './library-types';
import { usbTracker, type UsbVolumeKnown } from './usb-tracker.svelte';
import {
	buildUsbTree,
	parseUsbLibraryWire,
	parseUsbPaneId,
	UsbLibraryError,
	usbNodeTitle,
	usbRowsForNode,
	withUsbPresence,
	type UsbLibraryWire
} from './usb-row-wire';

export { UsbLibraryError } from './usb-row-wire';

const REMOVED_SUFFIX = ' (stick removed)';

/** What the stick tree renders for one stick. */
export type UsbStickView =
	| { status: 'loading' }
	| { status: 'ready'; name: string; tree: PlaylistNode[]; trackCount: number; readMs: number }
	| { status: 'error'; code: string; message: string };

export const usbLibrary = $state({
	/** By volume id (`vol:<uuid>`); present only for sticks that were opened. */
	sticks: {} as Record<string, UsbStickView>,
	/** Folder pane ids the user expanded; survives unplug and replug. */
	openFolders: {} as Record<string, boolean>
});

/** The parsed libraries themselves: large and immutable, so kept out of the
 * reactive graph. `usbLibrary.sticks` carries the small view state. */
const _libraries = new Map<string, UsbLibraryWire>();
const _inflight = new Map<string, Promise<UsbLibraryWire>>();
/** Bumped when a stick leaves, so a read that was in flight across the
 * unplug cannot repopulate the cache for a stick that is gone. */
const _epochs = new Map<string, number>();
/** Last seen presence of every stick that was opened. */
const _tracked = new Map<string, boolean>();
let _panesOf: (() => readonly PaneStore[]) | null = null;
let _stopWatch: (() => void) | null = null;

// ------------------------------------------------------------ helpers

/** VolumeUUID of a `vol:<uuid>` volume id. A `path:` id has no UUID, so no
 * stable track ids can be minted for it (USBPLAY-04): refuse, never guess. */
export function usbVolumeUuid(volumeId: string): string {
	if (!volumeId.startsWith('vol:') || volumeId.length === 4) {
		throw new UsbLibraryError(
			'USB_VOLUME_HAS_NO_UUID',
			`${volumeId} has no VolumeUUID, so its tracks cannot get stable ids`
		);
	}
	return volumeId.slice(4);
}

export function usbLibraryUrl(volumeUuid: string): string {
	return `/api/v1/usb/volumes/${encodeURIComponent(`vol:${volumeUuid}`)}/library`;
}

function _setView(volumeUuid: string, view: UsbStickView | null): void {
	if (view === null) delete usbLibrary.sticks[`vol:${volumeUuid}`];
	else usbLibrary.sticks[`vol:${volumeUuid}`] = view;
}

/** The backend's typed code, reworded for the one place a DJ reads it. */
function _stickError(exc: unknown): UsbLibraryError {
	if (exc instanceof UsbLibraryError) return exc;
	if (exc instanceof RbApiError) {
		// RbApiError's message is `${code}: ${detail}`; UsbLibraryError adds
		// the code back, so keep only the backend's detail sentence.
		const detail = exc.message.startsWith(`${exc.code}: `)
			? exc.message.slice(exc.code.length + 2)
			: exc.message;
		return new UsbLibraryError(
			exc.code,
			exc.code === 'USB_STICK_NOT_MOUNTED' ? 'Stick removed' : detail
		);
	}
	return new UsbLibraryError(
		'USB_LIBRARY_READ_FAILED',
		exc instanceof Error ? exc.message : String(exc)
	);
}

const USB_ERROR_HEADLINES: ReadonlyMap<string, string> = new Map([
	['USB_STICK_NOT_MOUNTED', 'Stick removed'],
	['USB_VOLUME_HAS_NO_UUID', 'no volume id: cannot browse this stick'],
	['usb_volume_discovery_unavailable', 'USB browsing is not available in this build']
]);

/** Short words for a stick failure code, as the stick tree shows them. */
export function usbErrorHeadline(code: string): string {
	return USB_ERROR_HEADLINES.get(code) ?? 'could not read this stick';
}

/** The toast a DJ reads for a stick failure: plain words for the known codes
 * (spec 4b: USB_STICK_NOT_MOUNTED reads "Stick removed"), and the typed cause
 * after the headline for anything else, so it is never hidden. */
export function usbErrorWords(error: UsbLibraryError): string {
	return USB_ERROR_HEADLINES.has(error.code)
		? usbErrorHeadline(error.code)
		: `${usbErrorHeadline(error.code)} (${error.message})`;
}

function _errorView(error: UsbLibraryError): UsbStickView {
	return { status: 'error', code: error.code, message: error.message };
}

// ------------------------------------------------------------ library cache

/** The stick's library, read once per VolumeUUID and then served from cache.
 * Concurrent callers share one request. */
export function ensureUsbLibrary(volumeUuid: string): Promise<UsbLibraryWire> {
	const cached = _libraries.get(volumeUuid);
	if (cached !== undefined) return Promise.resolve(cached);
	const pending = _inflight.get(volumeUuid);
	if (pending !== undefined) return pending;
	// Only a FIRST open records presence: a replug must stay visible to
	// applyUsbVolumePresence as a false -> true transition, whichever of the
	// tree remount or the volume watcher runs first.
	if (!_tracked.has(volumeUuid)) _tracked.set(volumeUuid, true);
	_setView(volumeUuid, { status: 'loading' });
	const read: Promise<UsbLibraryWire> = _readLibrary(
		volumeUuid,
		_epochs.get(volumeUuid) ?? 0
	).finally(() => {
		if (_inflight.get(volumeUuid) === read) _inflight.delete(volumeUuid);
	});
	_inflight.set(volumeUuid, read);
	return read;
}

async function _readLibrary(volumeUuid: string, epoch: number): Promise<UsbLibraryWire> {
	const current = (): boolean => (_epochs.get(volumeUuid) ?? 0) === epoch;
	try {
		const library = parseUsbLibraryWire(await fetchRbJson<unknown>(usbLibraryUrl(volumeUuid)));
		if (library.volume_uuid !== volumeUuid) {
			throw new UsbLibraryError(
				'USB_LIBRARY_MALFORMED',
				`asked for stick ${volumeUuid}, got ${library.volume_uuid}`
			);
		}
		if (!current()) throw new UsbLibraryError('USB_STICK_NOT_MOUNTED', 'Stick removed');
		// Cached before the tree is built: panes read the library, not the tree.
		_libraries.set(volumeUuid, library);
		const tree = buildUsbTree(library);
		if (tree.stranded.length > 0) {
			console.warn(
				`[usb] ${library.name.trim()}: ${tree.stranded.length} playlist(s) have no folder parent in the export and are shown at the root:`,
				tree.stranded
			);
		}
		_setView(volumeUuid, {
			status: 'ready',
			name: library.name.trim(),
			tree: tree.nodes,
			trackCount: library.tracks.length,
			readMs: library.read_ms
		});
		return library;
	} catch (exc) {
		const error = _stickError(exc);
		if (current()) _setView(volumeUuid, _errorView(error));
		throw error;
	}
}

/** Open a stick from the tree: read its library unless already cached. A
 * failure is recorded on the stick's view, which is where the tree shows it,
 * so the rejection is consumed here on purpose rather than dropped. */
export function openUsbStick(volume: Pick<UsbVolumeKnown, 'id'>): void {
	let volumeUuid: string;
	try {
		volumeUuid = usbVolumeUuid(volume.id);
	} catch (exc) {
		usbLibrary.sticks[volume.id] = _errorView(_stickError(exc));
		return;
	}
	watchUsbVolumes();
	ensureUsbLibrary(volumeUuid).catch((exc: unknown) => {
		console.warn(`[usb] ${volume.id} library read failed:`, exc);
	});
}

export function toggleUsbFolder(paneId: string): void {
	usbLibrary.openFolders[paneId] = !usbLibrary.openFolders[paneId];
}

// ------------------------------------------------------------ presence

/**
 * React to the volume list (USBPLAY-09). A tracked stick the list shows as
 * not present drops its cache and grays its panes in place; one shown present
 * again re-reads into its panes. A tracked stick the list does not name is
 * unknown, not unplugged: the tracker keeps every stick it has seen, present
 * or not, so absence only means it has not listed that stick yet. Returns the
 * VolumeUUIDs whose presence changed. Called by the watcher on every volume
 * poll; exported for tests, which cannot run Svelte effects.
 */
export function applyUsbVolumePresence(volumes: readonly UsbVolumeKnown[]): string[] {
	const listed = new Map(
		volumes.filter((v) => v.id.startsWith('vol:')).map((v) => [v.id.slice(4), v.present === true])
	);
	const changed: string[] = [];
	for (const [volumeUuid, wasPresent] of _tracked) {
		const isPresent = listed.get(volumeUuid);
		if (isPresent === undefined || isPresent === wasPresent) continue;
		_tracked.set(volumeUuid, isPresent);
		changed.push(volumeUuid);
		if (!isPresent) {
			_epochs.set(volumeUuid, (_epochs.get(volumeUuid) ?? 0) + 1);
			_libraries.delete(volumeUuid);
			_inflight.delete(volumeUuid);
			_setView(volumeUuid, null);
		}
		for (const pane of _panesOf?.() ?? []) {
			if (pane.kind !== 'usb' || pane.playlist_id === null || pane.loading) continue;
			if (parseUsbPaneId(pane.playlist_id)?.volumeUuid !== volumeUuid) continue;
			if (isPresent) {
				refreshUsbPane(pane).catch((exc: unknown) => {
					console.error(`[usb] ${pane.playlist_id} did not come back after replug:`, exc);
				});
			} else {
				const view = _removedView({ rows: pane.rows, title: pane.title });
				pane.rows = view.rows;
				pane.title = view.title;
			}
		}
	}
	return changed;
}

/** Arm the presence watcher once, on the first stick a user opens. Until
 * the tracker's first poll this session lands, its presence flags are the
 * last session's (restored from storage), so none of them is applied. */
export function watchUsbVolumes(): void {
	if (_stopWatch !== null) return;
	_stopWatch = $effect.root(() => {
		$effect(() => {
			// Tracks only the list and the poll stamp: the tracker reassigns
			// both on every poll, and the pane writes below must not
			// re-trigger this effect.
			const volumes = usbTracker.volumes;
			if (usbTracker.scannedAt === null) return;
			untrack(() => applyUsbVolumePresence(volumes));
		});
	});
}

// ------------------------------------------------------------ panes

interface PaneView {
	rows: BrowserRow[];
	title: string;
}

function _removedView(current: PaneView): PaneView {
	return {
		rows: withUsbPresence(current.rows, false),
		title: current.title.endsWith(REMOVED_SUFFIX)
			? current.title
			: `${current.title}${REMOVED_SUFFIX}`
	};
}

/** Rows and title for a stick pane. `shown` is what the pane already holds:
 * null on a first load, which fails with the typed error when the stick is
 * gone; a loaded pane grays what it shows instead. */
export async function usbPaneView(paneId: string, shown: PaneView | null): Promise<PaneView> {
	const ref = parseUsbPaneId(paneId);
	if (ref === null) {
		throw new UsbLibraryError('USB_PANE_ID_INVALID', `${paneId} is not a stick pane id`);
	}
	try {
		const library = await ensureUsbLibrary(ref.volumeUuid);
		return {
			rows: usbRowsForNode(library, ref.nodeKey, true),
			title: usbNodeTitle(library, ref.nodeKey)
		};
	} catch (exc) {
		const error = _stickError(exc);
		if (error.code === 'USB_STICK_NOT_MOUNTED' && shown !== null) return _removedView(shown);
		throw error;
	}
}

/**
 * BrowserPanel's stick branch of _loadPane. `panesOf` lets a later unplug or
 * replug find every pane showing this stick. Settles the load itself, through
 * the pane's load token so a newer selection always wins: rows on success,
 * the DJ's words on failure. Returns the toast for a failure that still owns
 * the pane, or null.
 */
export async function loadUsbPane(
	pane: PaneStore,
	seq: number,
	panesOf: () => readonly PaneStore[]
): Promise<string | null> {
	_panesOf = panesOf;
	watchUsbVolumes();
	try {
		const paneId = pane.playlist_id;
		if (paneId === null) throw new UsbLibraryError('USB_PANE_ID_INVALID', 'blank pane');
		const view = await usbPaneView(paneId, null);
		if (pane.completeLoad(seq, view.rows, false)) pane.title = view.title;
		return null;
	} catch (exc) {
		const words = usbErrorWords(_stickError(exc));
		return pane.failLoad(seq, words) ? words : null;
	}
}

/** Background refresh of a loaded stick pane, in place: keeps selection and
 * scroll, as BrowserPanel's library refresh does for its own panes. */
export async function refreshUsbPane(pane: PaneStore): Promise<void> {
	const requested = pane.playlist_id;
	if (requested === null || pane.loading) return;
	const view = await usbPaneView(requested, { rows: pane.rows, title: pane.title });
	if (pane.playlist_id !== requested || pane.loading) return;
	pane.rows = view.rows;
	pane.title = view.title;
	pruneSelection(pane, view.rows);
}
