/**
 * CUEOUT-21 (POC): tell the operator when other software is likely to be
 * fighting cue alignment for the microphone.
 *
 * Why this exists, measured Wed 16 Sep 2026: a Bluetooth headset could not be
 * calibrated because macOS kept re-selecting it as the system INPUT, which
 * drops the link from A2DP to HFP. In HFP the probe sweep does not survive:
 * the same sweep scored 0.54-0.65 per burst while A2DP held and 0.00-0.11
 * after the flip. Fathom's always-on monitor was re-claiming the input every
 * ~13 seconds. The calibration measured 0.09, reported it honestly, and then
 * blamed the microphone's POSITION, which is the one thing that was correct.
 *
 * A web page cannot see running processes, so the engine answers this. It is
 * a POC and deliberately a heuristic: it reports what is installed or running
 * and never claims to know who holds the device.
 *
 * Plain `fetch` rather than the generated client because the OpenAPI types
 * have not been regenerated for this route yet; that is the follow-up, not a
 * different design.
 */

export type AudioInterferenceKind = 'running_app' | 'audio_driver';

export interface AudioInterferenceItem {
	key: string;
	label: string;
	kind: AudioInterferenceKind;
	why: string;
	matched: string;
}

export interface AudioInterferenceReport {
	/** false means the engine could not look, which is NOT "nothing found". */
	supported: boolean;
	detected: AudioInterferenceItem[];
	error: string | null;
}

/** One line for the modal, or null when there is nothing worth saying.
 *
 * Pure so the copy can be tested without a server. An unsupported report says
 * nothing: a probe that could not run must not be rendered as a warning NOR
 * as an all-clear.
 */
function _list(names: readonly string[]): string {
	return names.length === 1
		? names[0]
		: `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

export function audioInterferenceWarning(report: AudioInterferenceReport | null): string | null {
	if (report === null || !report.supported || report.detected.length === 0) return null;
	// A running app and an installed HAL plug-in are different findings with
	// different remedies, so they get different sentences. Telling someone to
	// quit Krisp when the Krisp app is closed is both false and useless: the
	// plug-in is in the audio stack whether or not its app is open.
	const apps = report.detected.filter((d) => d.kind === 'running_app').map((d) => d.label);
	const drivers = report.detected.filter((d) => d.kind === 'audio_driver').map((d) => d.label);
	const why =
		`Software that listens to the microphone can take it mid-run, and on a ` +
		`Bluetooth headset that switches the link to call mode, where the ` +
		`calibration tone cannot be heard.`;
	const sentences: string[] = [];
	if (apps.length > 0) {
		sentences.push(`${_list(apps)} ${apps.length === 1 ? 'is' : 'are'} running.`);
	}
	if (drivers.length > 0) {
		sentences.push(
			`${_list(drivers)} ${drivers.length === 1 ? 'is' : 'are'} installed as an audio ` +
				`plug-in, which stays in the audio stack even with its app closed.`
		);
	}
	const fix =
		drivers.length === 0
			? 'Quit it before calibrating.'
			: apps.length === 0
				? 'Disable or remove it in its own settings before calibrating.'
				: 'Quit the app, and disable or remove the plug-in in its own settings, before calibrating.';
	return `${sentences.join(' ')} ${why} ${fix}`;
}

const AUDIO_INTERFERENCE_PATH = '/api/v1/audio-interference';

export const audioInterference = $state<{ report: AudioInterferenceReport | null }>({ report: null });

export async function refreshAudioInterference(fetcher: typeof fetch = fetch): Promise<void> {
	try {
		const res = await fetcher(AUDIO_INTERFERENCE_PATH);
		if (!res.ok) {
			audioInterference.report = { supported: false, detected: [], error: `HTTP ${res.status}` };
			return;
		}
		audioInterference.report = (await res.json()) as AudioInterferenceReport;
	} catch (error) {
		// Never block or fail a calibration over an advisory check.
		audioInterference.report = {
			supported: false,
			detected: [],
			error: error instanceof Error ? error.message : String(error)
		};
	}
}
