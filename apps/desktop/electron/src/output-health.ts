// Output-device health for the shell health server (issue #923, AUDIO-DEVICE-01).
//
// The Tauri shell probes CoreAudio in-process (src-tauri/src/output_health.rs,
// ~1,000 lines of FFI). This shell does not link CoreAudio. On macOS it runs
// `payload/bin/odj-output-probe <probe|switch>` when the payload carries that
// helper, and otherwise answers the honest ADR-0125 `unknown` verdict with the
// reason stated. Extracting output_health.rs into that helper is a blocker on
// the cutover plan (.planning/phases/20-electron-desktop-shell/20-01-PLAN.md).

import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';

import { utcTimestampIso } from './shell-log';

export const OUTPUT_HEALTH_PATH = '/api/v1/audio/output-health';
export const SWITCH_OUTPUT_PATH = '/api/v1/audio/switch-output';
export const OUTPUT_PROBE_HELPER = 'bin/odj-output-probe';

export type OutputAction = 'probe' | 'switch';

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

/**
 * Answer one output-health action. `payloadDir` null means an external engine
 * origin with no bundled payload, which also has no helper.
 */
export function outputHealthJson(action: OutputAction, payloadDir: string | null, platform = process.platform): string {
	const refuse = (reason: string): string =>
		action === 'probe' ? unknownProbeJson(reason) : switchUnavailableJson(reason);
	if (platform !== 'darwin') {
		return refuse(
			action === 'probe'
				? 'installed macOS shell required for OS output probe'
				: 'installed macOS shell required for output device cycling'
		);
	}
	const helper = payloadDir === null ? null : path.join(payloadDir, OUTPUT_PROBE_HELPER);
	if (helper === null || !fs.existsSync(helper)) {
		return refuse(`output probe helper not bundled (${OUTPUT_PROBE_HELPER}); the Electron shell does not link CoreAudio`);
	}
	const result = spawnSync(helper, [action], { encoding: 'utf8', timeout: 10_000 });
	if (result.status !== 0) {
		const why = result.error?.message ?? (result.stderr.trim() || `exit status ${String(result.status)}`);
		return refuse(`output probe helper failed: ${why}`);
	}
	try {
		JSON.parse(result.stdout);
		return result.stdout.trim();
	} catch {
		return refuse('output probe helper printed something that is not JSON');
	}
}
