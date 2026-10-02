/**
 * THE UPDATE CHANNEL -- is a newer Open DJ available, and can this window take it?
 *
 * TWO HALVES, DELIBERATELY SPLIT, mirroring apps/engine_core/update_channel.py:
 *
 *   ASKING   -- plain HTTP to the engine's GET /api/v1/update/check. Works in
 *               the desktop shell AND in a browser tab, and is the same
 *               question `python -m apps.engine_core.update_channel check`
 *               answers for an agent. No Tauri involved.
 *   APPLYING -- the desktop shell's Tauri updater, and NOTHING ELSE. The
 *               plugin re-fetches the manifest and verifies its minisign
 *               signature against the pubkey compiled into the binary. Nothing
 *               here downloads or installs, and nothing here hands the plugin
 *               a manifest this code parsed, because verification must not be
 *               separable from installation.
 *
 * A browser tab can therefore learn an update exists and cannot install it.
 * That is stated on screen rather than rendered as a dead button.
 *
 * NO SILENT NO-OP. The engine answers 502 with a named status when the channel
 * cannot be read, and every one of those statuses is rendered. A check that
 * quietly showed nothing when its endpoint was unreachable would turn an
 * outage into a false "you are current", which is the exact failure the
 * BuildIdentity readout beside it exists to prevent.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 fetchUpdateCheck: an answered channel yields the verdict; a 502, a
 *     dead socket or a non-JSON body each yield a fault carrying the reason.
 *     [if] an unreachable endpoint renders as "up to date" [then ⛔️] broken
 *   ✔︎ 🎯 summarizeUpdate: every status maps to a short label; an unknown
 *     status is a fault, never blank. A checkout or dev server must not shout
 *     UPDATE CHECK FAILED when the channel is missing; an installed app still
 *     must.
 *     [if] a status renders as an empty badge [then ⛔️] broken
 *     [if] a checkout renders UPDATE CHECK FAILED [then ⛔️] broken
 *   ✔︎ 🎯 canApplyHere: true only inside a Tauri shell, false in a browser.
 *     [if] a browser tab reports it can install [then ⛔️] broken
 *   ✔︎ 🎯 applyUpdate: refuses with a stated reason when there is no shell.
 *     [if] it resolves successfully with no shell [then ⛔️] broken
 */

import { API_BASE } from '$lib/api/base';
import {
	electronApplyUpdate,
	nativeShellKind,
	type ShellUpdateOutcome,
	type ShellUpdateProgress
} from '$lib/shell/native-shell';

/** The route the engine serves. Spelled once. */
export const UPDATE_CHECK_PATH = '/api/v1/update/check';

/** Statuses that mean the channel answered. Anything else is a fault. */
export const ANSWERED = ['update-available', 'up-to-date', 'ahead-of-channel'] as const;

export type UpdateStatus =
	| 'update-available'
	| 'up-to-date'
	| 'ahead-of-channel'
	| 'endpoint-unreachable'
	| 'endpoint-refused'
	| 'manifest-malformed'
	| 'platform-unsupported'
	| 'identity-unavailable';

/** Mirrors UpdateCheckOut in apps/engine_core/update_channel.py. */
export interface UpdateCheck {
	status: UpdateStatus;
	endpoint: string;
	platform_key: string;
	current_version: string | null;
	available_version: string | null;
	current_git_sha: string | null;
	current_built_at_utc: string | null;
	published_at: string | null;
	notes: string | null;
	same_version_different_build: boolean;
	detail: string | null;
	applies_via: string;
}

export type UpdateState =
	| { kind: 'idle' }
	| { kind: 'checking' }
	| { kind: 'ok'; value: UpdateCheck }
	| { kind: 'fault'; reason: string; value: UpdateCheck | null };

export function isAnswered(status: UpdateStatus): boolean {
	return (ANSWERED as readonly string[]).includes(status);
}

/**
 * Ask the engine what the channel is offering.
 *
 * A 502 is EXPECTED and is not an error in the transport sense: it is how the
 * engine reports a named channel fault, and its body still carries the full
 * UpdateCheck. So the body is parsed on both paths and the status field, not
 * the HTTP code, decides what is rendered.
 */
export async function fetchUpdateCheck(
	fetchImpl: typeof fetch = globalThis.fetch,
	base: string = API_BASE
): Promise<UpdateState> {
	let response: Response;
	try {
		response = await fetchImpl(`${base}${UPDATE_CHECK_PATH}`, {
			headers: { accept: 'application/json' },
			cache: 'no-store'
		});
	} catch (err) {
		const detail = err instanceof Error ? err.message : String(err);
		return {
			kind: 'fault',
			reason: `the engine could not be reached at ${UPDATE_CHECK_PATH} (${detail})`,
			value: null
		};
	}
	if (response.status === 404) {
		return {
			kind: 'fault',
			reason: `this daemon does not serve ${UPDATE_CHECK_PATH}; it predates the update channel`,
			value: null
		};
	}
	let body: unknown;
	try {
		body = await response.json();
	} catch {
		return {
			kind: 'fault',
			reason: `${UPDATE_CHECK_PATH} answered ${response.status} with a body that is not JSON`,
			value: null
		};
	}
	const check = body as Partial<UpdateCheck>;
	if (typeof check.status !== 'string') {
		return {
			kind: 'fault',
			reason: `${UPDATE_CHECK_PATH} answered ${response.status} without a status field`,
			value: null
		};
	}
	const value = check as UpdateCheck;
	if (isAnswered(value.status)) {
		return { kind: 'ok', value };
	}
	return {
		kind: 'fault',
		reason: value.detail ?? `the channel reported ${value.status}`,
		value
	};
}

/** Whether this build expects a working auto-update channel. */
export function isUpdaterExpected(opts: {
	isDev: boolean;
	engineSource: 'payload' | 'repo' | null;
	inTauri: boolean;
}): boolean {
	if (opts.isDev) return false;
	if (opts.inTauri) return true;
	if (opts.engineSource === 'repo') return false;
	if (opts.engineSource === 'payload') return true;
	return false;
}

// ----- rendering ----------------------------------------------------------
export interface UpdateSummary {
	/** The compact badge text. Never empty. */
	label: string;
	/** Whether this deserves the reader's attention in the collapsed tray. */
	prominent: boolean;
	/** The hover explanation, per house rule. */
	title: string;
}

/**
 * One status, one sentence. Every branch is spelled with an explicit `else if`
 * rather than a lookup with a default, so a status added to the engine and
 * forgotten here fails the exhaustiveness check instead of rendering blank.
 */
export function summarizeUpdate(
	state: UpdateState,
	opts: { updaterExpected?: boolean } = {}
): UpdateSummary | null {
	const updaterExpected = opts.updaterExpected ?? true;
	if (state.kind === 'idle') {
		return null;
	} else if (state.kind === 'checking') {
		return { label: 'checking...', prominent: false, title: 'Asking the update channel.' };
	} else if (state.kind === 'fault') {
		if (!updaterExpected) {
			return {
				label: 'dev build, no update channel',
				prominent: false,
				title:
					`This is a checkout or dev server, not an installed app, so a missing update channel is not a failure.\n\n` +
					`${state.reason}` +
					(state.value === null ? '' : `\n\nEndpoint: ${state.value.endpoint}`)
			};
		}
		return {
			label: 'UPDATE CHECK FAILED',
			prominent: true,
			title:
				`The update channel could not be read, so whether a newer Open DJ exists is UNKNOWN. ` +
				`This is not "you are up to date".\n\n${state.reason}` +
				(state.value === null ? '' : `\n\nEndpoint: ${state.value.endpoint}`)
		};
	}

	const check = state.value;
	const where = `Endpoint: ${check.endpoint}\nPlatform: ${check.platform_key}`;
	const running =
		`Running ${check.current_version ?? '?'} (${check.current_git_sha ?? '?'}` +
		`, built ${check.current_built_at_utc ?? '?'}).`;

	if (check.status === 'update-available') {
		return {
			label: `UPDATE ${check.available_version}`,
			prominent: true,
			title:
				`A newer Open DJ is available: ${check.current_version ?? '?'} -> ` +
				`${check.available_version}.\n\n${running}\n\nApplied by ${check.applies_via}.\n\n` +
				(check.notes ? `Notes: ${check.notes}\n\n` : '') +
				where
		};
	} else if (check.status === 'up-to-date') {
		return {
			label: check.same_version_different_build ? 'not the release build' : 'up to date',
			prominent: false,
			title: check.same_version_different_build
				? `The channel offers ${check.available_version}, the same VERSION this build reports, ` +
					`but its release notes do not name this build's commit. The updater compares version ` +
					`numbers, so it will not offer anything -- yet you may not be running the release.\n\n` +
					`${running}\n\n${where}`
				: `The channel offers ${check.available_version}, which is what this build already is.\n\n` +
					`${running}\n\n${where}`
		};
	} else if (check.status === 'ahead-of-channel') {
		return {
			label: 'ahead of channel',
			prominent: false,
			title:
				`This build (${check.current_version}) is NEWER than what the channel offers ` +
				`(${check.available_version}). That is normal for a development build and means the ` +
				`channel is behind, not that you are current.\n\n${running}\n\n${where}`
		};
	}
	// Faults are handled above; an answered status that reaches here is a
	// status the engine added and this module has not been taught.
	const unexpected: string = check.status;
	return {
		label: 'UNKNOWN UPDATE STATE',
		prominent: true,
		title: `The engine reported a status this UI does not know how to render: ${unexpected}`
	};
}

// ----- applying -----------------------------------------------------------
/**
 * Can THIS window install an update?
 *
 * Only inside a desktop shell (Tauri or Electron). A browser tab pointed at
 * the same engine reaches the same page and has no installer, and telling it
 * so is the whole reason this function exists rather than a try/catch around
 * the attempt.
 */
export function canApplyHere(scope: Record<string, unknown> = globalThis): boolean {
	return nativeShellKind(scope) !== null;
}

export type ApplyProgress = ShellUpdateProgress;

export type ApplyOutcome = ShellUpdateOutcome;

/**
 * Download, verify and install, then restart into the new build.
 *
 * The shell's updater (Tauri plugin, or electron-updater behind the Electron
 * bridge) does its OWN check here rather than being handed the one
 * the engine already did. That is deliberate and is not redundant work: the
 * plugin must fetch the manifest itself to verify its signature against the
 * compiled-in public key, and an installer that accepted a caller's parsed
 * manifest would have that verification bypassed by whoever called it.
 *
 * The plugin packages are imported DYNAMICALLY so they stay out of the main
 * bundle: this SPA is served to browsers too, and they can never run this path.
 */
export async function applyUpdate(
	onProgress: (progress: ApplyProgress) => void = () => {},
	scope: Record<string, unknown> = globalThis
): Promise<ApplyOutcome> {
	if (!canApplyHere(scope)) {
		return {
			kind: 'refused',
			reason:
				'this page is running in a browser, not in the Open DJ desktop shell, so it has no ' +
				'installer. Open the desktop app and check for updates there.'
		};
	}
	// Electron: the shell's own updater fetches, verifies and installs.
	const electron = electronApplyUpdate(onProgress, scope);
	if (electron !== null) {
		try {
			return await electron;
		} catch (err) {
			return { kind: 'refused', reason: err instanceof Error ? err.message : String(err) };
		}
	}
	let check: typeof import('@tauri-apps/plugin-updater').check;
	let relaunch: typeof import('@tauri-apps/plugin-process').relaunch;
	try {
		({ check } = await import('@tauri-apps/plugin-updater'));
		({ relaunch } = await import('@tauri-apps/plugin-process'));
	} catch (err) {
		return {
			kind: 'refused',
			reason: `the updater plugin could not be loaded: ${err instanceof Error ? err.message : String(err)}`
		};
	}

	try {
		onProgress({ phase: 'checking' });
		const update = await check();
		if (update === null) {
			return { kind: 'no-update' };
		}
		let received = 0;
		let total: number | null = null;
		await update.downloadAndInstall((event) => {
			if (event.event === 'Started') {
				total = event.data.contentLength ?? null;
				onProgress({ phase: 'downloading', received: 0, total });
			} else if (event.event === 'Progress') {
				received += event.data.chunkLength;
				onProgress({ phase: 'downloading', received, total });
			} else if (event.event === 'Finished') {
				onProgress({ phase: 'installing' });
			}
		});
		onProgress({ phase: 'restarting' });
		await relaunch();
		return { kind: 'installed' };
	} catch (err) {
		// Everything the updater can refuse arrives here: a signature that does
		// not verify, an endpoint that 404s, a bundle macOS will not replace.
		// Each is surfaced verbatim; none of them is retried silently.
		return {
			kind: 'refused',
			reason: err instanceof Error ? err.message : String(err)
		};
	}
}
