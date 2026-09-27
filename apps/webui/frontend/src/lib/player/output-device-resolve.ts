/**
 * Resolve a SAVED audio output against the outputs enumerated NOW (RESCUE-05).
 *
 * A saved `MediaDeviceInfo.deviceId` is not a stable name for a device. Chromium
 * (so WebView2) hashes it with a per-ORIGIN salt, and the desktop shell starts the
 * engine on a fresh OS-chosen loopback port every launch, so the page origin, and
 * with it every device id, changes on every relaunch. Measured Sat 26 Sep 2026 on
 * Edge 153 (the WebView2 153 engine) with
 * `apps/webui/frontend/scripts/probe-device-id-salting.mjs`: one physical output
 * reports a different id under two `127.0.0.1` ports and the SAME id for the same
 * origin across a browser restart; `groupId` differs across origins as well; the
 * label is identical; the `default` and `communications` pseudo-devices keep their
 * literal ids.
 *
 * So a saved output carries its label beside its id, and resolution runs: exact id,
 * then exact label when exactly ONE enumerated output carries it, otherwise nothing.
 * Two outputs sharing the saved label are never guessed between, because the wrong
 * guess sends the room mix to the wrong speakers. Decision record:
 * `docs/decisions/ADR-NEW-output-device-descriptor-survives-origin-change.md`.
 */

/** A persisted output choice: the id it had when saved, plus its label then. */
export interface SavedOutputDevice {
	device_id: string;
	/** Null when the label was hidden (no media permission) or never captured
	 * (a snapshot written before RESCUE-05). */
	label: string | null;
}

/** One output as enumerated now; structurally the mixer read model's entry. */
export interface EnumeratedOutputDevice {
	id: string;
	label: string;
}

export type OutputDeviceResolution =
	| { status: 'matched_id'; device_id: string }
	| { status: 'matched_label'; device_id: string }
	| { status: 'not_found' }
	| { status: 'ambiguous_label'; match_count: number };

export function resolveSavedOutputDevice(
	saved: SavedOutputDevice,
	outputs: readonly EnumeratedOutputDevice[]
): OutputDeviceResolution {
	if (typeof saved.device_id !== 'string' || saved.device_id.trim() === '') {
		throw new TypeError('saved output device_id must be a non-empty string');
	}
	if (saved.label !== null && typeof saved.label !== 'string') {
		throw new TypeError('saved output label must be a string or null');
	}
	if (outputs.some((output) => output.id === saved.device_id)) {
		return { status: 'matched_id', device_id: saved.device_id };
	}
	if (saved.label === null || saved.label.trim() === '') return { status: 'not_found' };
	const byLabel = outputs.filter((output) => output.label === saved.label);
	if (byLabel.length === 1) return { status: 'matched_label', device_id: byLabel[0].id };
	if (byLabel.length > 1) return { status: 'ambiguous_label', match_count: byLabel.length };
	return { status: 'not_found' };
}
