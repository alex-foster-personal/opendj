/**
 * USB export screen state (USBEXP-01, USBEXP-02, USBEXP-03).
 *
 * Drives the three server routes that already existed with no screen
 * (`/api/v1/usb-export/plan`, `/apply`, `/readback`) plus `/api/v1/usb/eject`.
 * The export they implement is narrow, and this module never claims more:
 * it writes ONE file, `PIONEER/rekordbox/exportLibrary.db`, onto an EMPTY
 * stick that carries the disposable marker, built from a rekordbox-made
 * template database. No audio files or waveforms are copied, so the result
 * is not a gig stick yet.
 *
 * Every refusal is shown with the daemon's own code and sentence. A write is
 * reported as done only after `/readback` answers `verified: true`.
 */
import type { components } from '../api-types';
import { usbExportRoutes } from '$lib/rb/api-writeback';
import { refreshUsbVolumes, usbRefreshSeq, usbTracker, type UsbDiscoveryState, type UsbVolumeKnown } from '$lib/rb/usb-tracker.svelte';
import { isQuitConfirmOpen, registerQuitVeto, subscribeQuitConfirmOpen } from '$lib/shell/quit-gate-state';

export type UsbExportPlan = components['schemas']['PlanModel'];
/** A plan whose envelope was checked: every playlist carries its track ids,
 * which the daemon always sends (see `checkPlanEnvelope`). */
export type CheckedUsbPlan = Omit<UsbExportPlan, 'playlists'> & {
	playlists: (components['schemas']['PlaylistModel'] & { track_ids: number[] })[];
};
type UsbExportReceipt = components['schemas']['ReceiptModel'];
export type UsbExportReadback = components['schemas']['ReadbackModel'];

export interface UsbProblem {
	code: string;
	message: string;
}

/** What an eject notice is about: the ejected stick's listing id, the other
 * sticks listed when the eject was sent, the newest refresh started before it
 * was sent (`sinceSeq`) and the newest started when it answered (`afterSeq`).
 * A listing that shows a stick plugged in since (a same-label replacement or
 * another stick) ends the notice, so "you can pull it out" never sits beside
 * a mounted device (Codex P2 4301877384, 4301908865). That counts from any
 * listing requested after the eject was sent, since one requested while it
 * ran may already show the replacement (Sol P1 4349545524). The ejected stick
 * itself counts only in a listing requested after the eject answered: one
 * requested earlier may still show it before it was ejected. */
export interface EjectWatch {
	id: string;
	others: string[];
	sinceSeq: number;
	afterSeq: number;
}

export type UsbExportStep = 'edit' | 'planning' | 'planned' | 'writing' | 'done';

/** The one device operation in flight. A plan, a write and an eject all talk
 * to the stick, so only one runs at a time, and while it runs the screen can
 * be neither closed nor re-targeted: a user who closes during a write would
 * see no progress and could pull the stick, and a second entry point would
 * reset state the first operation's late answer then overwrites. */
export interface UsbBusy {
	token: number;
	kind: 'plan' | 'write' | 'eject';
	/** What the screen says while it runs, e.g. "Writing to GIG-A". */
	label: string;
}

export const usbExport = $state({
	open: false,
	step: 'edit' as UsbExportStep,
	/** mount_path of the chosen stick; null until the user picks one. */
	targetPath: null as string | null,
	/** Listing id (`vol:<VolumeUUID>`) of the chosen stick, set with targetPath:
	 * a path alone cannot tell two sticks with the same label apart. */
	targetId: null as string | null,
	templatePath: '',
	playlistName: '',
	trackIdsText: '',
	plan: null as CheckedUsbPlan | null,
	readback: null as UsbExportReadback | null,
	problem: null as UsbProblem | null,
	/** mount_path currently being ejected, so only that row shows busy. */
	ejecting: null as string | null,
	ejectNotice: null as string | null,
	/** Which stick `ejectNotice` is about; see EjectWatch. */
	ejectWatch: null as EjectWatch | null,
	ejectProblem: null as UsbProblem | null,
	/** Non-null while a device operation runs; see UsbBusy. */
	busy: null as UsbBusy | null,
	/** Why the last close or entry was refused while busy; cleared when it settles. */
	busyNotice: null as string | null
});

let _lastToken = 0;

/** Claim the single operation slot, or refuse (null) and say why. */
function _begin(kind: UsbBusy['kind'], label: string): number | null {
	if (usbExport.busy !== null) {
		_refuseWhileBusy();
		return null;
	}
	_lastToken += 1;
	usbExport.busy = { token: _lastToken, kind, label };
	usbExport.busyNotice = null;
	return _lastToken;
}

/** True while `token` still owns the slot; a settled or foreign answer writes nothing. */
function _owns(token: number): boolean {
	return usbExport.busy?.token === token;
}

function _end(token: number): void {
	if (!_owns(token)) return;
	usbExport.busy = null;
	usbExport.busyNotice = null;
}

function _refuseWhileBusy(): void {
	const busy = usbExport.busy;
	if (busy === null) return;
	usbExport.open = true;
	usbExport.busyNotice = `${busy.label} - wait for it to finish before closing or starting anything else.`;
}

/** True while a plan, write or eject is in flight (the close paths are inert). */
export function usbExportBusy(): boolean {
	return usbExport.busy !== null;
}

/**
 * Refuse an in-app navigation that would unmount the screen while a device
 * operation runs (true = refused; the screen stays up and says why). A
 * client-side navigation that stays on the same route keeps the screen
 * mounted, so it is not refused; a document unload (a reload of that same
 * route, a window close, an external link) tears the screen down whatever the
 * route ids say, so it always is (Sol P1 4397722418). Nothing is refused once
 * the operation has settled.
 */
export function refuseNavigationWhileUsbBusy(
	fromRouteId: string | null,
	toRouteId: string | null,
	willUnload = false
): boolean {
	if (usbExport.busy === null) return false;
	if (!willUnload && fromRouteId !== null && fromRouteId === toRouteId) return false;
	_refuseWhileBusy();
	return true;
}

// The desktop shell's quit (window close, Cmd-Q) is not a page navigation, so
// it is refused here, the same way, while a plan, write or eject runs.
registerQuitVeto(() => refuseNavigationWhileUsbBusy(null, null));

/** The screen is a modal <dialog>: it sits in the browser's top layer and makes
 * the rest of the page inert, so the shell's quit confirm (an ordinary overlay)
 * would open unseen and unclickable beneath it while Enter still confirmed it.
 * An idle screen closes when the confirm opens (Codex P2 4293176283); the pick
 * and plan stay in this store. While an operation runs the quit is vetoed, so
 * the confirm never opens. */
export function closeForQuitConfirm(quitConfirmOpen: boolean): void {
	if (quitConfirmOpen && usbExport.open && usbExport.busy === null) usbExport.open = false;
}

subscribeQuitConfirmOpen(() => closeForQuitConfirm(isQuitConfirmOpen()));

// How many USB export screens are mounted right now. The screen counts itself
// in and out, so an action that needs the screen up (Eject from a stick's
// menu) can check it is there after the lazy chunk loads (Codex P1 4291127157).
let _screenMounts = 0;

/** Call when a USB export screen mounts; the returned function, on unmount. */
export function countUsbExportScreenMount(): () => void {
	_screenMounts += 1;
	let counted = true;
	return () => {
		if (!counted) return;
		counted = false;
		_screenMounts -= 1;
	};
}

export function usbExportScreenMounted(): boolean {
	return _screenMounts > 0;
}

/** A fresh open drops the last eject's outcome: it names no stick identity,
 * so after a close it would sit beside whatever stick is mounted now, a
 * same-label replacement included, and say "you can pull it out" about a
 * mounted device (Codex P2 4301471569). An open over a screen already up
 * keeps it. */
export function openUsbExport(): void {
	if (!usbExport.open) {
		usbExport.ejectNotice = null;
		usbExport.ejectWatch = null;
		usbExport.ejectProblem = null;
	}
	usbExport.open = true;
	void refreshUsbVolumes();
}

/** A playlist's "Export to USB..." (USBEXP-04): open the screen with its name.
 * Track ids stay empty: they come from the template, not from the library. */
export function openUsbExportForPlaylist(name: string): boolean {
	if (usbExport.busy !== null) {
		// Never re-target the screen under a running operation.
		_refuseWhileBusy();
		return false;
	}
	// Every playlist-menu invocation is a new export intent, even for a playlist
	// with the same name as the last one: a plan checked for that one must not
	// carry over (two playlists can share a name).
	usbExport.playlistName = name;
	usbExport.trackIdsText = '';
	resetUsbExportPlan();
	openUsbExport();
	return true;
}

/** Close the screen, unless a device operation is running: then it stays
 * visible with its progress, and the refusal says why (false). */
export function closeUsbExport(): boolean {
	if (usbExport.busy !== null) {
		_refuseWhileBusy();
		return false;
	}
	usbExport.open = false;
	return true;
}

/** The volumes the screen lists: present USB sticks, music or not. A blank
 * disposable stick is exactly an export target, and the tracker folds a
 * blank stick away as "not music", so the music verdict is not used here.
 * Nothing is listed unless discovery is healthy NOW: before this session's
 * first scan (`scannedAt` null) every `present` flag is last session's, and
 * once a later poll finds discovery unavailable or unreachable the last good
 * scan is stale. Either way a Write or Eject could hit a stick that is gone,
 * or another stick now at the same mount path. */
export function exportTargets(
	volumes: readonly UsbVolumeKnown[],
	tracker: { scannedAt: number | null; discovery: UsbDiscoveryState }
): UsbVolumeKnown[] {
	if (tracker.scannedAt === null || tracker.discovery !== 'ok') return [];
	return volumes.filter(
		(v) =>
			v.present === true &&
			v.role === 'usb_stick' &&
			// The scanner calls removable media on any bus a stick (an SD card in
			// a reader); export and eject both refuse a bus other than USB, so
			// only a listing that says USB is offered (Sol P1 4350543033).
			v.protocol === 'USB' &&
			v.simulated !== true &&
			typeof v.mount_path === 'string' &&
			v.mount_path !== ''
	);
}

/** True only when a healthy scan proves the picked stick is gone: discovery
 * answered ok this session and lists no present stick with that id at that
 * path. An outage (unknown, unavailable, unreachable) proves nothing, so the
 * pick and any result on screen stay put while the actions are disabled. */
export function pickedStickGone(
	volumes: readonly UsbVolumeKnown[],
	tracker: { scannedAt: number | null; discovery: UsbDiscoveryState },
	targetPath: string,
	targetId: string | null
): boolean {
	if (tracker.scannedAt === null || tracker.discovery !== 'ok') return false;
	return !exportTargets(volumes, tracker).some((v) => v.id === targetId && v.mount_path === targetPath);
}

/** The eject watch after one listing: null ends the notice. Only a healthy
 * listing requested after the eject was sent counts: an outage lists nothing
 * and proves nothing. The ejected stick counts only in a listing requested
 * after the eject answered, because the daemon drops its scan cache on a
 * proven eject before it answers, so only a later request is sure to read the
 * sticks as they are now. */
export function nextEjectWatch(
	watch: EjectWatch,
	volumes: readonly UsbVolumeKnown[],
	tracker: { scannedAt: number | null; discovery: UsbDiscoveryState; listingSeq: number }
): EjectWatch | null {
	if (tracker.scannedAt === null || tracker.discovery !== 'ok' || tracker.listingSeq <= watch.sinceSeq) return watch;
	const answered = tracker.listingSeq > watch.afterSeq;
	const ids = exportTargets(volumes, tracker).map((v) => v.id);
	return ids.some((id) => (id === watch.id ? answered : !watch.others.includes(id))) ? null : watch;
}

/** Follow the eject notice through a listing the screen just received. */
export function followEjectNotice(
	volumes: readonly UsbVolumeKnown[],
	tracker: { scannedAt: number | null; discovery: UsbDiscoveryState; listingSeq: number }
): void {
	const watch = usbExport.ejectWatch;
	if (watch === null) return;
	const next = nextEjectWatch(watch, volumes, tracker);
	if (next === watch) return;
	usbExport.ejectWatch = next;
	if (next === null) usbExport.ejectNotice = null;
}

export const NO_VOLUME_UUID =
	'not offered: the USB scan has no volume UUID for this stick, so Open DJ cannot prove a plan or an eject reaches this stick and not another one at the same path; eject it from the Finder';

/** Why Check plan, Write and Eject are not offered for a listed stick, or
 * null when they are. Every one of them is checked against the stick's volume
 * UUID: a listing id without one (the scanner's `path:` fallback) gives a plan
 * `planMatchesTarget` always discards and an eject the daemon always refuses
 * with eject_identity_unverifiable, so the stick stays listed but its actions
 * are disabled with this reason (Sol P1 4398681354). */
export function stickActionBlocker(volumeId: string | null): string | null {
	return volumeId !== null && volumeId.startsWith('vol:') && volumeId.length > 'vol:'.length ? null : NO_VOLUME_UUID;
}

/** True when a plan was built for the picked stick: its volume UUID is the
 * one in the listing id the user picked, and its target_root is the mount the
 * user picked. Another stick that mounted at the same path while the plan ran
 * gives a plan for a device nobody chose; a stale or version-skewed answer for
 * the same UUID at another root would arm a Write the apply route runs against
 * that other mount, which for two cloned sticks is the one not picked (Codex
 * 4352307431). */
export function planMatchesTarget(
	plan: Pick<UsbExportPlan, 'volume_uuid' | 'target_root'>,
	targetId: string | null,
	targetRoot: string
): boolean {
	if (targetId === null || !targetId.startsWith('vol:')) return false;
	const uuid = targetId.slice('vol:'.length);
	return uuid !== '' && plan.volume_uuid === uuid && targetRoot !== '' && plan.target_root === targetRoot;
}

/** "1, 2,3" -> [1, 2, 3]. Anything that is not a whole positive number is
 * an error naming the bad entry, never silently dropped. Empty -> []. */
export function parseTrackIds(text: string): number[] | UsbProblem {
	const parts = text
		.split(/[\s,]+/)
		.map((p) => p.trim())
		.filter((p) => p !== '');
	const ids: number[] = [];
	for (const part of parts) {
		// Above MAX_SAFE_INTEGER, Number() rounds to a different id (Sol P2 4287654324).
		const id = /^\d+$/.test(part) ? Number(part) : NaN;
		if (!Number.isSafeInteger(id) || id <= 0) {
			return {
				code: 'track_ids_invalid',
				message: `"${part}" is not a track id: use whole numbers from the template, separated by commas`
			};
		}
		ids.push(id);
	}
	return ids;
}

/** The plan answer's own envelope: a plan the screen shows and Write sends
 * back must carry every field `PlanModel` requires, with the types it
 * declares. A missing or mistyped field is contract drift, so it is refused
 * here rather than rendered (an empty playlist) or armed for a Write the
 * daemon would refuse as malformed, again and again (Sol P1 4349545535,
 * Codex 4349817101). */
const PLAN_STRINGS = [
	'plan_id',
	'scope',
	'template_path',
	'template_sha256',
	'target_root',
	'volume_label',
	'volume_uuid',
	'authorization_id',
	'output_relative_path'
] as const;
const TRACK_UPDATE_INTS = ['rating', 'bpmx100', 'color_id'] as const;
const TRACK_UPDATE_STRINGS = ['title', 'dj_comment'] as const;

function isRecord(v: unknown): v is Record<string, unknown> {
	return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function isOptional(v: unknown, ok: (x: unknown) => boolean): boolean {
	return v === undefined || v === null || ok(v);
}

const isInt = (v: unknown) => Number.isSafeInteger(v);
const isString = (v: unknown) => typeof v === 'string';

export function checkPlanEnvelope(plan: unknown): CheckedUsbPlan | UsbProblem {
	const bad: string[] = [];
	if (!isRecord(plan)) {
		bad.push('the plan');
	} else {
		for (const key of PLAN_STRINGS) if (!isString(plan[key])) bad.push(key);
		if (!isInt(plan.schema_version)) bad.push('schema_version');
		if (!Array.isArray(plan.playlists)) {
			bad.push('playlists');
		} else {
			plan.playlists.forEach((pl: unknown, i: number) => {
				if (!isRecord(pl)) return void bad.push(`playlists[${i}]`);
				if (!isString(pl.name)) bad.push(`playlists[${i}].name`);
				if (!Array.isArray(pl.track_ids) || !pl.track_ids.every(isInt)) bad.push(`playlists[${i}].track_ids`);
				if (!isOptional(pl.parent_id, isInt)) bad.push(`playlists[${i}].parent_id`);
			});
		}
		if (!Array.isArray(plan.track_updates)) {
			bad.push('track_updates');
		} else {
			plan.track_updates.forEach((u: unknown, i: number) => {
				if (!isRecord(u)) return void bad.push(`track_updates[${i}]`);
				if (!isInt(u.id)) bad.push(`track_updates[${i}].id`);
				for (const key of TRACK_UPDATE_INTS) if (!isOptional(u[key], isInt)) bad.push(`track_updates[${i}].${key}`);
				for (const key of TRACK_UPDATE_STRINGS) if (!isOptional(u[key], isString)) bad.push(`track_updates[${i}].${key}`);
			});
		}
	}
	if (bad.length === 0) return plan as CheckedUsbPlan;
	return {
		code: 'daemon_envelope_malformed',
		message: `the daemon's plan answer is missing or malformed: ${bad.join(', ')}`
	};
}

const isRecordList = (v: unknown) => Array.isArray(v) && v.every(isRecord);
const isIntList = (v: unknown) => Array.isArray(v) && v.every(isInt);
const isBoolean = (v: unknown) => typeof v === 'boolean';

/** The fields of the apply receipt (ReceiptModel) and the read-back
 * (ReadbackModel) the daemon always sends, each with its type check. */
const RECEIPT_FIELDS: Record<string, (v: unknown) => boolean> = {
	plan_id: isString,
	volume_uuid: isString,
	output_relative_path: isString,
	output_sha256: isString,
	playlist_ids: isIntList,
	verified: isBoolean
};
const READBACK_FIELDS: Record<string, (v: unknown) => boolean> = {
	plan_id: isString,
	volume_uuid: isString,
	output_relative_path: isString,
	output_sha256: isString,
	playlists: isRecordList,
	track_updates: isRecordList,
	verified: isBoolean
};
/** UsbEjectOut fields the daemon always sends; `ejected` must be the JSON literal
 * `true`, not a truthy string (Codex 4350837641). */
const EJECT_OUT_FIELDS: Record<string, (v: unknown) => boolean> = {
	ejected: (v) => v === true,
	name: isString,
	message: isString,
	mount_path: isString
};

function malformedFields(answer: unknown, fields: Record<string, (v: unknown) => boolean>, whole: string): string[] {
	if (!isRecord(answer)) return [whole];
	return Object.entries(fields)
		.filter(([key, ok]) => !ok(answer[key]))
		.map(([key]) => key);
}

/** ReceiptModel's fields that are missing or mistyped, or [] when the apply
 * answer has the shape the daemon always sends. The receipt is sent back to
 * /readback as is, so a malformed one is never forwarded (Codex 4350964742). */
export function malformedReceiptFields(receipt: unknown): string[] {
	return malformedFields(receipt, RECEIPT_FIELDS, 'the receipt');
}

/** ReadbackModel's fields that are missing or mistyped, or [] when the answer
 * has the shape the daemon always sends. The done line renders
 * `playlists.length`, so a string there would count its characters as
 * playlists verified (Codex 4350837630). */
export function malformedReadbackFields(readback: unknown): string[] {
	return malformedFields(readback, READBACK_FIELDS, 'the read-back');
}

/** The read-back evidence that does not name the reviewed plan's write:
 * plan, stick and path must match the plan and the apply receipt, and the
 * hash the receipt's. A verified read-back of some other write is not this
 * write's proof (Codex 4351495306). */
export function mismatchedReadbackEvidence(
	plan: Pick<UsbExportPlan, 'plan_id' | 'volume_uuid' | 'output_relative_path'>,
	receipt: Pick<UsbExportReceipt, 'plan_id' | 'volume_uuid' | 'output_relative_path' | 'output_sha256'>,
	readback: Pick<UsbExportReadback, 'plan_id' | 'volume_uuid' | 'output_relative_path' | 'output_sha256'>
): string[] {
	const fromPlan = (['plan_id', 'volume_uuid', 'output_relative_path'] as const).filter(
		(key) => receipt[key] !== plan[key] || readback[key] !== plan[key]
	);
	return readback.output_sha256 === receipt.output_sha256 ? fromPlan : [...fromPlan, 'output_sha256'];
}

/** A daemon error that names a code but carries no sentence is contract
 * drift, so it is shown as that, never as if the code were the sentence. */
function malformedEnvelope(daemonCode: string): UsbProblem {
	return {
		code: 'daemon_envelope_malformed',
		message: `the daemon answered ${daemonCode} without the sentence its error contract requires`
	};
}

function bodyShape(body: unknown): string {
	if (body === null || body === undefined) return 'none';
	if (Array.isArray(body)) return 'array';
	if (typeof body !== 'object') return typeof body;
	const keys = Object.keys(body);
	return keys.length === 0 ? 'empty object' : `object with ${keys.sort().join(', ')}`;
}

/** When a 200 eject answer is not a whole UsbEjectOut with `ejected: true` for the
 * mount asked, the stick stays plugged in and nothing says it is safe to pull. */
function usbEjectUnconfirmedProblem(body: unknown, mountPath: string): UsbProblem | null {
	if (!isRecord(body)) {
		return {
			code: 'eject_unconfirmed',
			message: `The daemon did not confirm the eject (${bodyShape(body)}). Keep the stick plugged in.`
		};
	}
	const bad = Object.entries(EJECT_OUT_FIELDS)
		.filter(([key, ok]) => !ok(body[key]))
		.map(([key]) => key);
	if (bad.length > 0) {
		return {
			code: 'eject_unconfirmed',
			message: `The daemon did not confirm the eject (${bodyShape(body)}${body.ejected !== undefined ? `, ejected: ${JSON.stringify(body.ejected)}` : ''}). Keep the stick plugged in.`
		};
	}
	if (body.mount_path !== mountPath) {
		return {
			code: 'eject_unconfirmed',
			message: `The daemon confirmed ejecting ${body.mount_path}, not ${mountPath}. Keep the stick plugged in.`
		};
	}
	return null;
}

/** A code or sentence the error contract can show: a string with words in
 * it. A blank or whitespace-only one would render as nothing (Sol P1 4351562923). */
function isWords(value: unknown): value is string {
	return typeof value === 'string' && value.trim() !== '';
}

/** One sentence for any failure, from either error envelope the daemon uses:
 * `{"detail": {code, message}}` (USB routes) or `{"error", "message"}` (the
 * one-way import gate's 403). An HTTP answer with neither envelope, or an
 * envelope with a code and no sentence, is refused as malformed, never "". */
export function describeUsbProblem(exc: unknown): UsbProblem {
	if (exc !== null && typeof exc === 'object') {
		const e = exc as { name?: unknown; status?: unknown; message?: unknown; body?: unknown };
		const body = e.body as
			| { detail?: { code?: unknown; message?: unknown; reason?: unknown } | unknown; error?: unknown; message?: unknown }
			| null
			| undefined;
		const detail = body?.detail;
		if (detail !== null && typeof detail === 'object' && !Array.isArray(detail)) {
			const d = detail as { code?: unknown; message?: unknown; reason?: unknown };
			if (isWords(d.code)) {
				const message = isWords(d.message) ? d.message : isWords(d.reason) ? d.reason : null;
				return message === null ? malformedEnvelope(d.code) : { code: d.code, message };
			}
		}
		if (body && isWords(body.error)) {
			return isWords(body.message)
				? { code: body.error, message: body.message }
				: malformedEnvelope(body.error);
		}
		if (typeof e.status === 'number') {
			// An HTTP answer with neither envelope is contract drift, not a refusal
			// the daemon worded: name it, keeping the status and shape as context.
			return {
				code: 'daemon_envelope_malformed',
				message: `the daemon answered HTTP ${e.status} without an error envelope (body: ${bodyShape(body)})`
			};
		}
		if (typeof e.message === 'string' && e.message !== '') {
			return { code: 'daemon_unreachable', message: e.message };
		}
	}
	return { code: 'unknown_error', message: String(exc) };
}

/** Any edit to the inputs voids a reviewed plan: apply must run the exact
 * plan the user looked at, so a stale one is dropped rather than reused. */
export function resetUsbExportPlan(): void {
	// A running plan or write owns these fields until it settles.
	if (usbExport.busy !== null) return;
	_resetPlan();
}

/** The picked stick left a healthy scan: drop the pick and its plan, but keep
 * what a finished write said about the stick (a warning or a verified readback);
 * that matters most exactly when the stick was pulled out during the write. */
export function forgetVanishedTarget(): void {
	if (usbExport.busy !== null) return;
	_clearTargetKeepingResult();
}

/** Drops the picked stick and its unwritten plan, keeping what a finished write
 * left: an after-write warning and a verified readback outlive the stick, both
 * when it leaves the scan and when it is ejected (Codex P2 4300993872). */
function _clearTargetKeepingResult(): void {
	usbExport.targetPath = null;
	usbExport.targetId = null;
	if (usbExport.problem !== null && !AFTER_WRITE_PROBLEMS.has(usbExport.problem.code)) usbExport.problem = null;
	// A verified write keeps its spent plan: the done line names the stick it
	// wrote from it, and Write is not offered once done (Codex P2 4293421286).
	if (usbExport.step === 'done') return;
	usbExport.plan = null;
	usbExport.step = 'edit';
}

function _resetPlan(): void {
	usbExport.plan = null;
	usbExport.readback = null;
	usbExport.problem = null;
	usbExport.step = 'edit';
}

export async function planUsbExport(): Promise<void> {
	if (usbExport.busy !== null) {
		_refuseWhileBusy();
		return;
	}
	usbExport.problem = null;
	usbExport.readback = null;
	usbExport.plan = null;
	if (usbExport.targetPath === null || usbExport.targetId === null) {
		usbExport.problem = { code: 'target_missing', message: 'Pick a USB stick to write to.' };
		return;
	}
	if (stickActionBlocker(usbExport.targetId) !== null) {
		usbExport.problem = { code: 'target_unverifiable', message: `Check plan is ${stickActionBlocker(usbExport.targetId)}.` };
		return;
	}
	if (usbExport.templatePath.trim() === '') {
		usbExport.problem = {
			code: 'template_missing',
			message: 'Give the path of a rekordbox-made exportLibrary.db to build from.'
		};
		return;
	}
	const ids = parseTrackIds(usbExport.trackIdsText);
	if (!Array.isArray(ids)) {
		usbExport.problem = ids;
		return;
	}
	const name = usbExport.playlistName.trim();
	if (name === '' && ids.length > 0) {
		usbExport.problem = { code: 'playlist_name_missing', message: 'Name the playlist those tracks go in.' };
		return;
	}
	const token = _begin('plan', 'Checking the plan');
	if (token === null) return;
	usbExport.step = 'planning';
	const targetRoot = usbExport.targetPath;
	const targetId = usbExport.targetId;
	try {
		const answer = await usbExportRoutes.plan({
			template_path: usbExport.templatePath.trim(),
			target_root: targetRoot,
			playlists: name === '' ? [] : [{ name, track_ids: ids }],
			track_updates: []
		});
		if (!_owns(token)) return;
		const plan = checkPlanEnvelope(answer);
		if (!('plan_id' in plan)) {
			usbExport.problem = plan;
			usbExport.step = 'edit';
			return;
		}
		if (usbExport.targetPath !== targetRoot || usbExport.targetId !== targetId || !planMatchesTarget(plan, targetId, targetRoot)) {
			// The stick was pulled or another was picked while the plan ran;
			// the screen could not reset a busy plan, so drop it here.
			usbExport.problem = {
				code: 'target_changed',
				message: 'The stick changed while the plan was checked. Pick the stick and check the plan again.'
			};
			usbExport.step = 'edit';
			return;
		}
		usbExport.plan = plan;
		usbExport.step = 'planned';
	} catch (exc) {
		if (!_owns(token)) return;
		usbExport.problem = describeUsbProblem(exc);
		usbExport.step = 'edit';
	} finally {
		_end(token);
	}
}

/** Apply the reviewed plan, then read it back. Only a readback with
 * `verified: true` reaches 'done'; anything else is a problem on screen. */
export async function writeUsbExport(): Promise<void> {
	const plan = usbExport.plan;
	if (plan === null) return;
	const token = _begin('write', `Writing to ${plan.volume_label}`);
	if (token === null) return;
	usbExport.problem = null;
	usbExport.step = 'writing';
	try {
		const receipt: UsbExportReceipt = await usbExportRoutes.apply({ plan, confirmation: plan.plan_id });
		if (!_owns(token)) return;
		const badReceipt = malformedReceiptFields(receipt);
		if (badReceipt.length > 0) {
			// A 200 means the write ran, so the plan is spent; a receipt that is
			// not ReceiptModel's is not forwarded to /readback (Codex 4350964742).
			usbExport.problem = {
				code: 'written_receipt_malformed',
				message: `The stick was written but the daemon's write receipt is malformed (${badReceipt.join(', ')}), so it was not read back.`
			};
			usbExport.plan = null;
			usbExport.step = 'edit';
			return;
		}
		let readback: UsbExportReadback;
		try {
			readback = await usbExportRoutes.readback({ plan, receipt });
		} catch (exc) {
			if (!_owns(token)) return;
			// The write landed; only the check failed. Say so, and drop the plan:
			// writing it again would only be refused as a non-empty stick.
			const why = describeUsbProblem(exc);
			usbExport.problem = {
				code: 'written_readback_failed',
				message: `The stick was written (${receipt.output_relative_path}) but reading it back failed: ${why.code}: ${why.message}`
			};
			usbExport.plan = null;
			usbExport.step = 'edit';
			return;
		}
		if (!_owns(token)) return;
		// Literal true only: a malformed answer with a truthy non-boolean
		// (the string "false") must not read as verified (Codex 4350696747),
		// and neither may a read-back whose other fields are not ReadbackModel's
		// (Codex 4350837630).
		const malformed = malformedReadbackFields(readback);
		const mismatched = malformed.length > 0 ? [] : mismatchedReadbackEvidence(plan, receipt, readback);
		if (receipt.verified !== true || readback.verified !== true || malformed.length > 0 || mismatched.length > 0) {
			// The write landed, so the plan is spent: writing it again would only
			// be refused as a non-empty stick and bury this warning.
			usbExport.problem = {
				code: 'readback_unverified',
				message:
					malformed.length > 0
						? `The stick was written but the read-back answer is malformed (${malformed.join(', ')}), so nothing is verified.`
						: mismatched.length > 0
							? `The stick was written but the read-back describes another write (${mismatched.join(', ')} differ from the reviewed plan), so nothing is verified.`
							: 'The stick was written but reading it back did not match the plan.'
			};
			usbExport.plan = null;
			usbExport.step = 'edit';
			return;
		}
		usbExport.readback = readback;
		usbExport.step = 'done';
	} catch (exc) {
		if (!_owns(token)) return;
		const why = describeUsbProblem(exc);
		if (SPENT_AFTER_WRITE.has(why.code)) {
			// The daemon refused after it wrote: the plan is spent, and its own
			// code and sentence say what was left on the stick (Sol P1 4349117035).
			usbExport.problem = why;
			usbExport.plan = null;
			usbExport.step = 'edit';
			return;
		}
		if (applyOutcomeUnknown(exc)) {
			// No answer, or the daemon failed mid-request: the write may have
			// started. Retire the plan so it cannot be written again blind.
			usbExport.problem = {
				code: 'write_outcome_unknown',
				message: `The write to ${plan.volume_label} may have started before it failed (${why.code}: ${why.message}). Check the stick, then plan again.`
			};
			usbExport.plan = null;
			usbExport.step = 'edit';
			return;
		}
		usbExport.problem = why;
		usbExport.step = 'planned';
	} finally {
		_end(token);
	}
}

/** Refusal codes the daemon raises AFTER it started writing to the stick, so
 * the plan is spent even though the answer is a 4xx: rollback_refused (the
 * export was promoted and could not be undone), and promotion_failed /
 * promotion_hash_mismatch (the export folders were created on the stick, so a
 * retry of the same plan would only be refused as target_not_empty; Codex
 * 4291212605), and target_output_exists (raised when another writer's
 * exportLibrary.db lands during promotion, after the folders were created; when
 * raised before writing, the stick already holds an export, so the plan can
 * never be written either way; Codex P2 4293492013), and target_changed (the
 * export folders appeared between the last emptiness check and the mkdir, so
 * every retry would only be refused as target_not_empty; Codex P2 4300723944). */
const SPENT_AFTER_WRITE = new Set([
	'rollback_refused',
	'promotion_failed',
	'promotion_hash_mismatch',
	'target_output_exists',
	'target_changed'
]);

/** Problems a finished write leaves behind: the stick may have been changed,
 * so they must outlive the stick leaving the scan (Codex P2 4293176272). */
const AFTER_WRITE_PROBLEMS = new Set([
	'write_outcome_unknown',
	'written_readback_failed',
	'readback_unverified',
	// A 200 apply ran the write, so a malformed receipt is kept like the
	// other unverified writes when the stick leaves (Codex 4351287674).
	'written_receipt_malformed',
	...SPENT_AFTER_WRITE
]);

/** True when an apply failure says nothing about whether the stick was
 * touched: a transport failure (no HTTP status), a 5xx, or a malformed
 * envelope, which could be a post-write code that lost its sentence. A
 * well-formed 4xx is the daemon refusing before it writes, so the plan is
 * still good to retry, except a code in SPENT_AFTER_WRITE, which the daemon
 * raises after it wrote. */
export function applyOutcomeUnknown(exc: unknown): boolean {
	if (exc === null || typeof exc !== 'object') return true;
	const status = (exc as { status?: unknown }).status;
	if (typeof status !== 'number' || status >= 500) return true;
	// FastAPI's request validation (a 422 listing field errors) answers before
	// the route runs, so nothing was written; it is still described as a
	// malformed envelope, since it carries no daemon code (Sol P1 4349117026).
	const body = (exc as { body?: unknown }).body;
	if (status === 422 && body !== null && typeof body === 'object' && Array.isArray((body as { detail?: unknown }).detail)) {
		return false;
	}
	const code = describeUsbProblem(exc).code;
	return code === 'daemon_envelope_malformed' || SPENT_AFTER_WRITE.has(code);
}

/** Ejects the stick the caller picked: `volumeId` is its listing id, and the
 * daemon refuses when the volume now mounted at `mountPath` is another one. */
export async function ejectUsbStick(mountPath: string, volumeId: string): Promise<void> {
	const token = _begin('eject', `Ejecting ${mountPath}`);
	if (token === null) return;
	usbExport.ejecting = mountPath;
	usbExport.ejectNotice = null;
	usbExport.ejectWatch = null;
	usbExport.ejectProblem = null;
	// Taken before the request goes out: a refresh started while the eject
	// runs can land first and list a replacement, which must neither join
	// `others` nor be skipped as too old (Sol P1 4349545524).
	const sinceSeq = usbRefreshSeq();
	const others = exportTargets(usbTracker.volumes, usbTracker)
		.map((v) => v.id)
		.filter((id) => id !== volumeId);
	let ejected = false;
	try {
		const body: unknown = await usbExportRoutes.eject(mountPath, volumeId);
		if (!_owns(token)) return;
		const unconfirmed = usbEjectUnconfirmedProblem(body, mountPath);
		if (unconfirmed !== null) {
			usbExport.ejectProblem = unconfirmed;
			return;
		}
		const confirmed = body as { name: string; message: string; mount_path: string };
		usbExport.ejectNotice = `${confirmed.name} ejected. You can pull it out.`;
		usbExport.ejectWatch = { id: volumeId, others, sinceSeq, afterSeq: usbRefreshSeq() };
		// The listing already on screen may come from a refresh that ran
		// alongside the eject; judge it now rather than at the next one.
		followEjectNotice(usbTracker.volumes, usbTracker);
		if (usbExport.targetPath === mountPath) _clearTargetKeepingResult();
		ejected = true;
	} catch (exc) {
		if (!_owns(token)) return;
		usbExport.ejectProblem = describeUsbProblem(exc);
	} finally {
		if (_owns(token)) usbExport.ejecting = null;
		_end(token);
	}
	// The stick is already safe to pull, so the rescan that drops it from the
	// list runs after the slot is released: a slow or stalled scan cannot keep
	// the screen locked on "keep it plugged in" (Codex P2 4287034882).
	if (ejected) void refreshUsbVolumes();
}
