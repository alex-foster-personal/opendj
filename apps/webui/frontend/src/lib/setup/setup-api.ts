/**
 * The first-run setup surface, through the generated client.
 *
 * Every call here has a one-to-one endpoint in apps/engine_core/setup/api.py,
 * which is the point: the wizard is a renderer for those five endpoints and an
 * agent can drive the identical flow with curl. Nothing in this module invents
 * state the daemon does not report.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 every call goes through `api` + `unwrap`, so the URL and the response
 *     type come from apps/webui/openapi.json rather than a string literal.
 *     [if] a raw fetch( appears in this file [then ⛔️] broken
 *   ✔︎ 🎯 setupRefusal() gates every request on the daemon flavor, because the
 *     legacy daemon does not serve /api/v1/setup and a request there is a
 *     guaranteed 404 that teaches the user nothing.
 *     [if] a legacy boot issues a setup request at all [then ⛔️] broken
 *   ✔︎ 🎯 startImport() surfaces the server's refusal code verbatim, so the
 *     wizard can tell "no rekordbox" from "already running".
 *     [if] a 409 is reworded into a generic failure [then ⛔️] broken
 */

import {
	agentAccessDetail,
	agentBlockerDetail,
	humanAccessCaveat,
	humanBlockerSentence,
	humanFolderVerdict,
	humanLegacySetupRefusal,
	humanSetupMissing,
	humanSetupProbePending
} from './present';
import type { components } from '../api-types';
import { capabilities } from '../api/capabilities.svelte';
import { api, unwrap } from '../api/client';

export type SetupStatus = components['schemas']['SetupStatusOut'];
export type RekordboxDetection = components['schemas']['RekordboxDetectionOut'];
export type StemsSetup = components['schemas']['StemsSetupOut'];
export type StemTier = components['schemas']['StemTierOut'];
export type FileProbe = components['schemas']['FileProbeOut'];
export type Permissions = components['schemas']['PermissionsOut'];
export type AccessProbe = components['schemas']['AccessProbeOut'];
export type SetupImportOptions = components['schemas']['SetupImportIn'];
export type FolderImportOptions = components['schemas']['FolderImportIn'];
export type FolderScan = components['schemas']['FolderScanOut'];
export type FolderCandidates = components['schemas']['FolderCandidatesOut'];
export type SetupJob = components['schemas']['JobOut'];
export type LastImport = NonNullable<SetupStatus['last_import']>;

/** Mirrors detect.CODES in apps/engine_core/setup/detect.py. A code outside
 * this list is contract drift, so the wizard shows the server's message rather
 * than a branch it does not have. */
export const SETUP_CODES = [
	'rekordbox_not_found',
	'rekordbox_key_unavailable',
	'rekordbox_share_missing',
	'rekordbox_decrypt_failed',
	'rekordbox_ingest_failed',
	'setup_import_already_running',
	'music_folder_access_denied'
] as const;

export type SetupCode = (typeof SETUP_CODES)[number];

/** Mirrors STAGES in apps/engine_core/setup/importer.py. Only used to label a
 * stage the server has actually reported; the authoritative list arrives on
 * SetupStatus.stages. */
export const STAGE_LABELS: Record<string, string> = {
	detect: 'Find your collection',
	snapshot: 'Copy it somewhere safe to read',
	decrypt: 'Unlock the collection copy',
	ingest: 'Read tracks and playlists into the library',
	analysis: 'Check waveform data is reachable'
};

/** The folder import's stages. Fewer, because there is no database to
 * snapshot and nothing to decrypt. */
export const FOLDER_STAGE_LABELS: Record<string, string> = {
	detect: 'Check the folders can be read',
	scan: 'Walk them for audio files',
	ingest: 'Read tags into the library (no analysis)'
};

const SETUP_MISSING =
	'setup API not offered by this daemon (no /api/v1/setup on a legacy boot)';
const UNIDENTIFIED = 'daemon not identified yet: GET /api/v1/health has not answered';

/**
 * Why the setup surface is inert, or null when the daemon offers it.
 *
 * Same shape as jobsRefusal(): one function that both gates the request and
 * supplies the tooltip, so a disabled control cannot disagree with the reason
 * it is disabled. Deliberately NOT the PARITY-TODO wording, which means "not
 * built"; this is built and simply is not served here.
 */
export function setupRefusal(): string | null {
	if (capabilities.flavor === 'engine') return null;
	return capabilities.flavor === 'legacy' ? SETUP_MISSING : UNIDENTIFIED;
}

/**
 * Why the setup surface is FINALLY refused, or null when it is not.
 *
 * Deliberately narrower than `setupRefusal()`, which folds two very different
 * answers into one string: "this daemon does not serve setup" (final) and
 * "the health probe has not answered yet" (temporary). A SURFACE that renders
 * the second one has just told a first-run user their setup failed, in the
 * first tick after load, on a perfectly healthy engine -- and then disabled
 * the buttons that would have fixed it. That is the exact failure the wizard
 * overlay exists to remove, so components gate on THIS and render the
 * unfinished probe as a scanning state instead.
 *
 * Same rule `runSetupBlocked()` already applies to the entry-point button;
 * that function now delegates here so the two cannot drift.
 */
export function finalSetupRefusal(): string | null {
	return capabilities.flavor === 'legacy' ? humanLegacySetupRefusal() : null;
}

/** Raw agent diagnostic for a final refusal, or null. */
export function finalSetupRefusalAgent(): string | null {
	return capabilities.flavor === 'legacy' ? SETUP_MISSING : null;
}

/** True while the daemon flavor is still unresolved, so a surface can say
 * "checking" instead of inventing a verdict. */
export function setupProbePending(): boolean {
	return capabilities.flavor === 'unknown';
}

export async function getSetupStatus(): Promise<SetupStatus> {
	return unwrap(api.GET('/api/v1/setup/status'));
}

export async function detectRekordbox(): Promise<RekordboxDetection> {
	return unwrap(api.GET('/api/v1/setup/detect/rekordbox'));
}

export async function getStemsSetup(): Promise<StemsSetup> {
	return unwrap(api.GET('/api/v1/setup/stems'));
}

/** Which music folders this engine can actually read.
 *
 * macOS answers a blocked directory listing with an EMPTY listing rather
 * than an error, so a library that is merely unreadable looks identical to
 * one that is genuinely empty. Anything showing a count has to show this
 * alongside it.
 */
export async function getPermissions(): Promise<Permissions> {
	return unwrap(api.GET('/api/v1/setup/permissions'));
}

export async function getFolderCandidates(): Promise<FolderCandidates> {
	return unwrap(api.GET('/api/v1/setup/detect/music-folders'));
}

/** The caveat sentence for a count, or null when nothing was blocked.
 *
 * HONEST DENOMINATORS: a track or file count taken while a folder was
 * unreadable is a count of what we were allowed to see, and it must say so
 * rather than presenting itself as the whole library.
 */
export function accessCaveat(permissions: Permissions | null): string | null {
	return humanAccessCaveat(permissions);
}

export { agentAccessDetail };

/** Enqueue the import. Returns the queued job row, whose id the wizard then
 * watches through the jobs store rather than polling here. */
export async function startImport(options: SetupImportOptions): Promise<SetupJob> {
	return unwrap(api.POST('/api/v1/setup/import', { body: options }));
}

/** Look inside a candidate folder WITHOUT importing it.
 *
 * `audio_files` is only a real count when `denied` is false. A denied folder
 * answers 0 because macOS refused the listing, and rendering that 0 as
 * "empty" is the exact failure this endpoint exists to prevent.
 */
export async function scanFolder(path: string): Promise<FolderScan> {
	return unwrap(
		api.GET('/api/v1/setup/detect/folder', { params: { query: { path } } })
	);
}

/** Enqueue a folder import: tags only, no analysis, and it says so.
 *
 * Accepts multiple absolute roots in one job; the first-run wizard posts every
 * validated row when the operator adds more than one music folder.
 */
export async function startFolderImport(
	options: FolderImportOptions
): Promise<SetupJob> {
	return unwrap(api.POST('/api/v1/setup/import/folder', { body: options }));
}

/** Canonical setup folder path on the client: trim and strip a trailing slash.
 *
 * Does not expand ``~``; the server refuses tilde paths at the wire boundary.
 * Mirrors ``normalise_path_prefix`` for display and duplicate detection.
 */
export function normalizeSetupFolderPath(path: string): string {
	const trimmed = path.trim();
	if (trimmed === '/') return trimmed;
	if (
		trimmed.length === 3 &&
		trimmed.charAt(1) === ':' &&
		(trimmed.charAt(2) === '/' || trimmed.charAt(2) === '\\')
	) {
		return trimmed;
	}
	return trimmed.replace(/[/\\]+$/, '');
}

/** What a scanned folder means, in one sentence a human can act on.
 *
 * The denied branch never quotes the file count: a count taken behind a
 * permission wall is a count of nothing, not a count of the folder.
 */
export function folderVerdict(scan: FolderScan): string {
	return humanFolderVerdict(scan);
}

/** True when this folder can actually be imported. */
export function folderIsImportable(scan: FolderScan | null): boolean {
	return scan !== null && scan.readable && scan.audio_files > 0;
}

/** Skip the wizard, or re-arm it. Persisted engine-side, so an agent reading
 * /api/v1/setup/status sees the same answer this tab does. */
export async function setDismissed(dismissed: boolean): Promise<SetupStatus> {
	return unwrap(api.POST('/api/v1/setup/dismiss', { body: { dismissed } }));
}

/** Human sentence for a detection blocker code.
 *
 * The server already sends prose with every refusal; these are for the codes
 * that arrive on `detection.blockers`, which is a list of codes only. An
 * unrecognised code is shown as itself rather than hidden.
 */
export function blockerSentence(code: string, detection: RekordboxDetection): string {
	return humanBlockerSentence(code, detection);
}

export function blockerAgentDetail(code: string, detection: RekordboxDetection): string {
	return agentBlockerDetail(code, detection);
}

/** True when a blocker stops the import outright.
 *
 * A missing share dir is not one of those: the tracks land either way and only
 * the waveforms are missing, so refusing the whole import over it would deny a
 * usable library for a problem that can be fixed afterwards. The server draws
 * the same line in start_import.
 */
export function isFatalBlocker(code: string): boolean {
	return code !== 'rekordbox_share_missing';
}

/** Bytes as something a human reads. Returns null for an absent file so a
 * caller renders nothing rather than "0 B". */
export function formatBytes(bytes: number | null | undefined): string | null {
	if (bytes === null || bytes === undefined) return null;
	if (bytes < 1024) return `${bytes} B`;
	const units = ['KB', 'MB', 'GB'];
	let value = bytes / 1024;
	let unit = 0;
	while (value >= 1024 && unit < units.length - 1) {
		value /= 1024;
		unit += 1;
	}
	return `${value.toFixed(1)} ${units[unit]}`;
}
