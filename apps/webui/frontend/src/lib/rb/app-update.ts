/**
 * RESTART TO UPDATE -- a local build an agent flagged as ready (INSTALL-39/40).
 *
 * Mirrors apps/engine_core/local_update.py. The engine records ONE pending
 * update (`opendj update ready <App.app.tar.gz>`); this module reads it for
 * the tray's nudge, and the click asks the engine to install it, which is the
 * same request `opendj update install` and the MCP `app_update_install` make.
 * The engine then hands the desktop shell a manifest URL over the shell
 * command bus, and `installLocalUpdate` (run by shell-commands.ts in the
 * shell's own webview) has the Tauri updater verify the archive's signature
 * against the compiled-in key, replace the bundle, and relaunch.
 *
 * One path for the click and the agent: the button never invokes the shell
 * itself, so an install from the UI and one from an agent cannot diverge.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 summarizePending: only state 'pending' yields a nudge; none,
 *     installed and artifact-changed yield none. The tooltip names the
 *     build's short sha, version and build time.
 *     [if] a nudge shows when nothing is pending [then ⛔️] broken
 *     [if] the tooltip omits the short sha [then ⛔️] broken
 *   ✔︎ 🎯 requestInstall: a refusal carries the engine's code and detail.
 *     [if] a 409 from install reads as accepted [then ⛔️] broken
 *   ✔︎ 🎯 installLocalUpdate: refuses with a stated reason outside Tauri.
 *     [if] it resolves installed with no Tauri shell [then ⛔️] broken
 *   ✔︎ 🎯 INSTALL-43 summarizeCountdown / advanceCancelCombo: an install
 *     counting down shows "Updating to <sha> in N s. Press Esc Space ×4 to
 *     cancel."; exactly Esc Space x4 completes the combo.
 *     [if] Esc Space x3 completes the combo [then ⛔️] broken
 *     [if] a claimed install still shows the countdown [then ⛔️] broken
 */

import { API_BASE } from '$lib/api/base';
import type { Lineage } from '$lib/rb/build-identity';
import { nativeShellKind, type ShellUpdateOutcome } from '$lib/shell/native-shell';

export const APP_UPDATE_STATUS_PATH = '/api/v1/app-update/status';
export const APP_UPDATE_INSTALL_PATH = '/api/v1/app-update/install';
export const APP_UPDATE_CANCEL_PATH = '/api/v1/app-update/cancel';
/**
 * How often the tray re-reads the record. An agent can flag a build, or start
 * an install with a 10 s countdown (INSTALL-43), at any moment, so the
 * countdown overlay must appear within a second of the CLI or MCP call.
 */
export const APP_UPDATE_POLL_MS = 1_000;

export type AppUpdateState = 'none' | 'pending' | 'installed' | 'artifact-changed';

export interface PendingBuild {
	artifact: string;
	app_version: string;
	git_sha_full: string;
	git_sha: string;
	built_at_utc: string;
	release_channel: string | null;
	notes: string | null;
	flagged_at_utc: string;
	size_bytes: number;
	sha256: string;
	lineage?: Lineage | null;
	descent?: Descent | null;
}

/** Mirrors DescentOut (INSTALL-44): does the flagged build contain the running one? */
export interface Descent {
	verdict: 'descended' | 'not-descended' | 'unverified' | 'unknown';
	running_git_sha_full: string | null;
	previous_git_sha_full: string | null;
	missing_commits: number | null;
	missing_from: string | null;
	warning: string | null;
}

/** The descent warning to show, and whether it is the loud one. Null when it descends. */
export function descentWarning(status: AppUpdateStatus | null): { text: string; loud: boolean } | null {
	const descent = status?.pending?.descent ?? null;
	if (descent === null || descent.warning === null) return null;
	return { text: descent.warning, loud: descent.verdict === 'not-descended' };
}

/** Mirrors UpdateApplyStatusOut: the newest local install order (INSTALL-43). */
export interface InstallOrder {
	command_id: string;
	state: 'pending' | 'claimed' | 'succeeded' | 'failed';
	outcome: 'installed' | 'no-update' | 'refused' | 'cancelled' | null;
	error: string | null;
	countdown_ends_at_utc: string | null;
	countdown_remaining_s: number | null;
}

/** Mirrors AppUpdateStatusOut. */
export interface AppUpdateStatus {
	state: AppUpdateState;
	pending: PendingBuild | null;
	running_git_sha_full: string | null;
	running_app_version: string | null;
	detail: string | null;
	install?: InstallOrder | null;
}

export interface Nudge {
	label: string;
	title: string;
}

export type InstallRequest =
	| { kind: 'accepted'; commandId: string }
	| { kind: 'refused'; reason: string };

/** The record, or null when the engine cannot say (the nudge then stays hidden). */
export async function fetchAppUpdateStatus(
	fetchImpl: typeof fetch = globalThis.fetch,
	base: string = API_BASE
): Promise<AppUpdateStatus | null> {
	try {
		const response = await fetchImpl(`${base}${APP_UPDATE_STATUS_PATH}`, {
			headers: { accept: 'application/json' },
			cache: 'no-store'
		});
		if (!response.ok) return null;
		const body = (await response.json()) as Partial<AppUpdateStatus>;
		return typeof body.state === 'string' ? (body as AppUpdateStatus) : null;
	} catch {
		return null;
	}
}

/** The nudge for this record, or null. Only a pending build is news. */
export function summarizePending(status: AppUpdateStatus | null): Nudge | null {
	if (status === null || status.state !== 'pending' || status.pending === null) return null;
	const build = status.pending;
	return {
		label: 'Restart to update',
		title:
			`A new build is ready: ${build.git_sha} (v${build.app_version}, built ${build.built_at_utc}` +
			`${build.release_channel ? `, ${build.release_channel}` : ''}).` +
			(build.notes ? `\n\n${build.notes}` : '') +
			`\n\nClick to install it and restart. The desktop shell verifies the package signature ` +
			`before replacing the app, and keeps the previous build as a backup.` +
			`\n\nRunning: ${status.running_git_sha_full ?? '?'}` +
			(build.descent?.warning ? `\n\n${build.descent.warning}` : '')
	};
}

/** How long the relaunched app shows "Updated to <sha>" (INSTALL-8338b). */
export const UPDATED_CONFIRM_MS = 8_000;

/** Per-viewer memory of the confirmation already shown, so it shows once per build. */
export const UPDATED_SEEN_KEY = 'opendj.app-update.confirmed-sha';

/**
 * "Updated to <sha>" once the build that was flagged is the one running, and
 * this viewer has not seen it for that build yet. Null otherwise.
 */
export function summarizeInstalled(status: AppUpdateStatus | null, seenSha: string | null): Nudge | null {
	if (status === null || status.state !== 'installed' || status.pending === null) return null;
	const build = status.pending;
	if (build.git_sha_full === seenSha) return null;
	return {
		label: `Updated to ${build.git_sha}`,
		title: `Open DJ restarted on build ${build.git_sha} (v${build.app_version}, built ${build.built_at_utc}).`
	};
}

// ----- install countdown and its cancel combo (INSTALL-43) ----------------
// the maintainer, Tue 3 Nov 2026: an agent's build installs immediately, even mid-play;
// the ONLY cancel is Esc Space Esc Space Esc Space Esc Space. The app has
// focus while it plays, so the webview sees the keys: no Input Monitoring.

/** The combo, in order. `' '` is the Space key's `KeyboardEvent.key`. */
export const CANCEL_COMBO: readonly string[] = [
	'Escape', ' ', 'Escape', ' ', 'Escape', ' ', 'Escape', ' '
];
export const CANCEL_COMBO_LABEL = 'Esc Space ×4';

/** Keys the countdown swallows, so the combo never also pauses a deck or closes a panel. */
export function isComboKey(key: string): boolean {
	return key === 'Escape' || key === ' ';
}

/**
 * Progress through CANCEL_COMBO after one key. A wrong key starts over (an
 * Escape counts as a fresh first key); a key-repeat changes nothing.
 */
export function advanceCancelCombo(progress: number, key: string, repeat = false): number {
	if (repeat) return progress;
	if (key === CANCEL_COMBO[progress]) return progress + 1;
	return key === CANCEL_COMBO[0] ? 1 : 0;
}

export function comboComplete(progress: number): boolean {
	return progress >= CANCEL_COMBO.length;
}

export interface Countdown {
	commandId: string;
	gitSha: string;
	secondsLeft: number;
	text: string;
	/** INSTALL-44: "NOT DESCENDED FROM YOUR LAST BUILD: ..." shown under the countdown. */
	warning: string | null;
}

/**
 * The overlay for an install counting down, or null. `elapsedMs` is how long
 * ago `status` was fetched, so the number ticks between polls.
 */
export function summarizeCountdown(status: AppUpdateStatus | null, elapsedMs = 0): Countdown | null {
	const order = status?.install ?? null;
	if (order === null || order.state !== 'pending' || order.countdown_remaining_s === null) return null;
	const gitSha = status?.pending?.git_sha ?? 'the new build';
	const secondsLeft = Math.max(0, Math.ceil(order.countdown_remaining_s - elapsedMs / 1000));
	return {
		commandId: order.command_id,
		gitSha,
		secondsLeft,
		warning: status?.pending?.descent?.warning ?? null,
		text:
			secondsLeft > 0
				? `Updating to ${gitSha} in ${secondsLeft} s. Press ${CANCEL_COMBO_LABEL} to cancel.`
				: `Updating to ${gitSha} now...`
	};
}

/** True when the newest install order was withdrawn during its countdown. */
export function installWasCancelled(status: AppUpdateStatus | null): boolean {
	return status?.install?.outcome === 'cancelled';
}

/** Withdraw the install counting down: the same request as `opendj update cancel`. */
export async function requestCancel(
	reason: string,
	fetchImpl: typeof fetch = globalThis.fetch,
	base: string = API_BASE
): Promise<InstallRequest> {
	let response: Response;
	try {
		response = await fetchImpl(`${base}${APP_UPDATE_CANCEL_PATH}`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ reason })
		});
	} catch (err) {
		return { kind: 'refused', reason: `the engine could not be reached (${String(err)})` };
	}
	const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
	if (response.ok && typeof body.command_id === 'string') {
		return { kind: 'accepted', commandId: body.command_id };
	}
	return {
		kind: 'refused',
		reason: `${String(body.error ?? `HTTP ${response.status}`)}: ${String(body.detail ?? '')}`
	};
}

/** Click phase of the nudge: idle, installing (engine accepted not yet), restarting (shell has it). */
export type InstallPhase = 'idle' | 'installing' | 'restarting';

export function installPhaseLabel(phase: InstallPhase, idleLabel: string): string {
	switch (phase) {
		case 'idle':
			return idleLabel;
		case 'installing':
			return 'Installing update...';
		case 'restarting':
			return 'Restarting...';
		default: {
			const _exhaustive: never = phase;
			throw new Error(`Unhandled install phase: ${_exhaustive}`);
		}
	}
}

/** Ask the engine to install the pending build: the click and the agent share this. */
export async function requestInstall(
	fetchImpl: typeof fetch = globalThis.fetch,
	base: string = API_BASE
): Promise<InstallRequest> {
	let response: Response;
	try {
		response = await fetchImpl(`${base}${APP_UPDATE_INSTALL_PATH}`, { method: 'POST' });
	} catch (err) {
		return { kind: 'refused', reason: `the engine could not be reached (${String(err)})` };
	}
	let body: Record<string, unknown> = {};
	try {
		body = (await response.json()) as Record<string, unknown>;
	} catch {
		return { kind: 'refused', reason: `install answered HTTP ${response.status} without JSON` };
	}
	if (response.status === 202 && typeof body.command_id === 'string') {
		return { kind: 'accepted', commandId: body.command_id };
	}
	return {
		kind: 'refused',
		reason: `${String(body.error ?? `HTTP ${response.status}`)}: ${String(body.detail ?? '')}`
	};
}

/**
 * Run by the desktop shell's command poll: the Tauri command verifies and
 * installs, then the window relaunches into the new build.
 */
export async function installLocalUpdate(
	manifestUrl: string,
	scope: Record<string, unknown> = globalThis
): Promise<ShellUpdateOutcome> {
	if (nativeShellKind(scope) !== 'tauri') {
		return {
			kind: 'refused',
			reason: 'installing a local build needs the Tauri desktop shell; this page has none'
		};
	}
	try {
		const { invoke } = await import('@tauri-apps/api/core');
		const { relaunch } = await import('@tauri-apps/plugin-process');
		await invoke('install_local_update', { manifestUrl });
		await relaunch();
		return { kind: 'installed' };
	} catch (err) {
		return { kind: 'refused', reason: err instanceof Error ? err.message : String(err) };
	}
}
