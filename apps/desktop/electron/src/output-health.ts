// Output-device health for the shell health server (issue #923, AUDIO-DEVICE-01).
//
// The CoreAudio probe (~1,000 lines of FFI) lives in its own crate,
// apps/desktop/native/odj-output-probe (src/output_health.rs), which the Tauri
// shell links in process. This shell does not link CoreAudio. On macOS it runs
// that crate's `payload/bin/odj-output-probe <probe|switch|list>` when the
// payload carries the helper, and otherwise answers the honest ADR-0125
// `unknown` verdict with the reason stated.

import { execFile, spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';

import { utcTimestampIso } from './shell-log';

export const OUTPUT_HEALTH_PATH = '/api/v1/audio/output-health';
export const SWITCH_OUTPUT_PATH = '/api/v1/audio/switch-output';
export const RECONNECT_SPEAKER_PATH = '/api/v1/audio/reconnect-speaker';
export const INPUT_DEVICES_PATH = '/api/v1/audio/input-devices';
export const OUTPUT_PROBE_HELPER = 'bin/odj-output-probe';
/** AUDIO-DEVICE-05: blueutil disconnect + reconnect + CoreAudio reappear, worst case about 30 s. */
export const RECONNECT_TIMEOUT_MS = 45_000;

/** Actions the helper answers fast enough to run synchronously. */
export type SyncOutputAction = 'probe' | 'switch' | 'list' | 'inputs';
export type OutputAction = SyncOutputAction | 'reconnect';

export function unknownProbeJson(reason: string): string {
	return JSON.stringify({
		device_delivering: null,
		verdict: 'unknown',
		reason,
		default_device_name: null,
		default_device_uid: null,
		io_cycles_advanced: null,
		hal_overload_recent: null,
		probe_available: false,
		checked_at: utcTimestampIso()
	});
}

export function switchUnavailableJson(error: string): string {
	return JSON.stringify({ cycled: false, error });
}

type ProbeRun =
	| { ok: true; stdout: string }
	| { ok: false; reason: string };

function runOutputProbeHelper(
	action: SyncOutputAction,
	payloadDir: string | null,
	platform: string
): ProbeRun {
	if (platform !== 'darwin') {
		return {
			ok: false,
			reason:
				action === 'list'
					? 'installed macOS shell required for CoreAudio output listing'
					: action === 'probe'
						? 'installed macOS shell required for OS output probe'
						: action === 'inputs'
							? 'installed macOS shell required to list CoreAudio inputs'
							: 'installed macOS shell required for output device cycling'
		};
	}
	const helper = payloadDir === null ? null : path.join(payloadDir, OUTPUT_PROBE_HELPER);
	if (helper === null || !fs.existsSync(helper)) {
		return {
			ok: false,
			reason: `output probe helper not bundled (${OUTPUT_PROBE_HELPER}); the Electron shell does not link CoreAudio`
		};
	}
	const result = spawnSync(helper, [action], { encoding: 'utf8', timeout: 10_000 });
	if (result.status !== 0) {
		const why = result.error?.message ?? (result.stderr.trim() || `exit status ${String(result.status)}`);
		return { ok: false, reason: `output probe helper failed: ${why}` };
	}
	return { ok: true, stdout: result.stdout.trim() };
}

/**
 * Read-only CoreAudio output descriptors for CUEOUT-26 under Electron.
 * Non-macOS returns null so Linux/Windows Electron keeps browser-only listing.
 */
export function coreAudioOutputDevices(payloadDir: string | null, platform = process.platform): unknown[] | null {
	if (platform !== 'darwin') return null;
	const run = runOutputProbeHelper('list', payloadDir, platform);
	if (!run.ok) throw new Error(run.reason);
	try {
		const parsed: unknown = JSON.parse(run.stdout);
		if (!Array.isArray(parsed)) {
			throw new Error('output probe helper list did not return a JSON array');
		}
		return parsed;
	} catch (error) {
		const detail = error instanceof Error ? error.message : String(error);
		throw new Error(
			detail.includes('JSON array') ? detail : 'output probe helper list printed something that is not JSON'
		);
	}
}

/**
 * Answer one output-health action. `payloadDir` null means an external engine
 * origin with no bundled payload, which also has no helper.
 */
export function outputHealthJson(action: SyncOutputAction, payloadDir: string | null, platform = process.platform): string {
	const refuse = (reason: string): string =>
		action === 'probe'
			? unknownProbeJson(reason)
			: action === 'inputs'
				? JSON.stringify({ inputs: [], error: reason })
				: switchUnavailableJson(reason);
	const run = runOutputProbeHelper(action, payloadDir, platform);
	if (!run.ok) return refuse(run.reason);
	try {
		JSON.parse(run.stdout);
		return run.stdout;
	} catch {
		return refuse('output probe helper printed something that is not JSON');
	}
}

export function reconnectUnavailableJson(error: string): string {
	return JSON.stringify({ reconnected: false, error });
}

/**
 * AUDIO-DEVICE-05: `odj-output-probe reconnect`. Asynchronous on purpose: the
 * Bluetooth disconnect and reconnect take seconds, and a spawnSync here would
 * freeze the Electron main process for all of them.
 */
export function reconnectSpeakerJson(payloadDir: string | null, platform = process.platform): Promise<string> {
	if (platform !== 'darwin') {
		return Promise.resolve(reconnectUnavailableJson('installed macOS shell required to reconnect a Bluetooth speaker'));
	}
	const helper = payloadDir === null ? null : path.join(payloadDir, OUTPUT_PROBE_HELPER);
	if (helper === null || !fs.existsSync(helper)) {
		return Promise.resolve(
			reconnectUnavailableJson(`output probe helper not bundled (${OUTPUT_PROBE_HELPER}); cannot reconnect the speaker`)
		);
	}
	return new Promise((resolve) => {
		execFile(helper, ['reconnect'], { encoding: 'utf8', timeout: RECONNECT_TIMEOUT_MS }, (error, stdout, stderr) => {
			if (error !== null) {
				resolve(reconnectUnavailableJson(`output probe helper failed: ${stderr.trim() || error.message}`));
				return;
			}
			try {
				JSON.parse(stdout);
				resolve(stdout.trim());
			} catch {
				resolve(reconnectUnavailableJson('output probe helper printed something that is not JSON'));
			}
		});
	});
}
