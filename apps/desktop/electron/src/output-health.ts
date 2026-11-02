// Output-device health for the shell health server (issue #923, AUDIO-DEVICE-01).
//
// The Tauri shell probes CoreAudio in-process (src-tauri/src/output_health.rs,
// ~1,000 lines of FFI). This shell does not link CoreAudio. On macOS it runs
// `payload/bin/odj-output-probe <probe|switch|list>` when the payload carries that
// helper, and otherwise answers the honest ADR-0125 `unknown` verdict with the
// reason stated. Extracting output_health.rs into that helper is a blocker on
// the cutover plan ((private path)).

import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';

import { utcTimestampIso } from './shell-log';

export const OUTPUT_HEALTH_PATH = '/api/v1/audio/output-health';
export const SWITCH_OUTPUT_PATH = '/api/v1/audio/switch-output';
export const OUTPUT_PROBE_HELPER = 'bin/odj-output-probe';

export type OutputAction = 'probe' | 'switch' | 'list';

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
	action: OutputAction,
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
export function outputHealthJson(action: OutputAction, payloadDir: string | null, platform = process.platform): string {
	const refuse = (reason: string): string =>
		action === 'probe' ? unknownProbeJson(reason) : switchUnavailableJson(reason);
	const run = runOutputProbeHelper(action, payloadDir, platform);
	if (!run.ok) return refuse(run.reason);
	try {
		JSON.parse(run.stdout);
		return run.stdout;
	} catch {
		return refuse('output probe helper printed something that is not JSON');
	}
}
