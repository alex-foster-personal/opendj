/**
 * #328 USB stick tracker - client store of known/seen volumes.
 *
 * Backend lists present mounts via GET /api/v1/usb/volumes (read-only).
 * Local metadata (yours / music / name / forgotten) persists in localStorage.
 * Never writes to a USB mount from this pass.
 */
import { RB_API_BASE } from '$lib/rb/api-rb';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import { pushToast } from '$lib/stores.svelte';

const STORAGE_KEY = 'mdt.rb.usb-volumes.v1';
const POLL_MS = 5000;

export type UsbKind = 'rekordbox' | 'djay' | 'music' | 'unknown';
export type UsbRole = 'usb_stick' | 'mounted_drive' | 'disk_image' | 'other';

export interface UsbVolumeKnown {
	id: string;
	name: string;
	mount_path?: string;
	is_music?: boolean;
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
};

type ApiList = { volumes: ApiVolume[]; scanned_at: number };

// ----- state -------------------------------------------------------------

export const usbTracker = $state({
	volumes: _load(),
	panelOpen: false,
	promptId: null as string | null,
	polling: false,
	lastError: null as string | null
});

let _pollTimer: ReturnType<typeof setInterval> | null = null;
let _started = false;

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

export function openUsbPanel(): void {
	usbTracker.panelOpen = true;
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
	if (patch.is_music !== undefined) row.is_music = patch.is_music;
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

export function startUsbWatch(): void {
	if (_started) return;
	_started = true;
	usbTracker.polling = true;
	void refreshUsbVolumes();
	_pollTimer = setInterval(() => {
		void refreshUsbVolumes();
	}, POLL_MS);
}

export function stopUsbWatch(): void {
	_started = false;
	usbTracker.polling = false;
	if (_pollTimer !== null) {
		clearInterval(_pollTimer);
		_pollTimer = null;
	}
}

export async function refreshUsbVolumes(): Promise<void> {
	try {
		const r = await fetch(`${RB_API_BASE}/api/v1/usb/volumes`, {
			headers: { Accept: 'application/json' }
		});
		if (!r.ok) {
			usbTracker.lastError = `usb volumes HTTP ${r.status}`;
			return;
		}
		const body = (await r.json()) as ApiList;
		_ingest(body.volumes ?? []);
		usbTracker.lastError = null;
	} catch (exc) {
		usbTracker.lastError = exc instanceof Error ? exc.message : String(exc);
	}
}

/** DEV: POST a fake volume so you can exercise UI without a stick. */
export async function simulateUsbVolume(opts?: {
	name?: string;
	kind?: UsbKind;
}): Promise<void> {
	const r = await fetch(`${RB_API_BASE}/api/v1/usb/volumes`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({
			name: opts?.name ?? 'FAKE USB',
			kind: opts?.kind ?? 'music',
			mount_path: '/Volumes/FAKE-USB',
			role: 'usb_stick',
			protocol: 'USB'
		})
	});
	if (!r.ok) throw new Error(`simulate USB HTTP ${r.status}`);
	const body = (await r.json()) as ApiList;
	_ingest(body.volumes ?? []);
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
		const autoNonMusic = role === 'mounted_drive' || role === 'disk_image' || api.is_music === false;
		if (prev === undefined) {
			const row: UsbVolumeKnown = {
				id: api.id,
				name: api.name,
				mount_path: api.mount_path ?? undefined,
				is_music: autoNonMusic ? false : api.is_music,
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
			prev.mount_path = api.mount_path ?? prev.mount_path;
			prev.kind = api.kind;
			prev.role = role;
			prev.protocol = api.protocol ?? prev.protocol ?? null;
			prev.simulated = api.simulated;
			if (api.hide_reason) prev.hide_reason = api.hide_reason;
			if (autoNonMusic) {
				prev.is_music = false;
				prev.needs_prompt = false;
			} else if (prev.is_music === undefined) {
				prev.is_music = api.is_music;
			}
			// Keep user rename; only fill empty.
			if (!prev.name) prev.name = api.name;
		}
	}

	for (const row of byId.values()) {
		if (!seen.has(row.id)) row.present = false;
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
