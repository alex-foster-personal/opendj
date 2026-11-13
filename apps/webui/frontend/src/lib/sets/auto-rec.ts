/** SETREC-11802b: start REC once on the first play of a /performance session. */

import { rustMode } from '$lib/audio-engine/rust-mode.svelte';
import { getRecorderStatus } from '../../routes/sets/sets-api';
import { setRecordingEnabled } from './recording-flag';
import {
	defaultChoiceForMode,
	getRememberedInput,
	initialRecordSelection,
	listRecorderDevices,
	masterMixUnavailableReason,
	startPerformanceRecorder,
	type RecordInputChoice,
	type RecordMode
} from './record-input-choice';

/** The build switch. `vite.config.ts` sets it in ONE place: `on` unless
 *  `MUSIC_DJ_SET_AUTO_REC=off`. A browser suite sharing one engine sets it
 *  off: a recording outlives the page that started it, stalls when that page
 *  closes, and every later page then shows the failed recording. Unset (a node
 *  harness with no bundler define) keeps the SETREC-11802b default, on. */
type SetAutoRecFlag = 'on' | 'off';

export function setAutoRecFlag(configured: unknown): SetAutoRecFlag {
	if (configured === undefined) return 'on';
	if (configured !== 'on' && configured !== 'off') {
		throw new TypeError(`VITE_SET_AUTO_REC must be on or off, got ${String(configured)}`);
	}
	return configured;
}

let autoRecEnabled =
	setAutoRecFlag(import.meta.env === undefined ? undefined : import.meta.env.VITE_SET_AUTO_REC) ===
	'on';
let autoRecConsumed = false;

/** Test seam: default on in production (SETREC-11802b). */
export function setSetAutoRecEnabledForTest(enabled: boolean): void {
	autoRecEnabled = enabled;
}

export function resetSetAutoRecSession(): void {
	autoRecConsumed = false;
}

export function setAutoRecConsumedForTest(consumed: boolean): void {
	autoRecConsumed = consumed;
}

export function autoRecSessionConsumed(): boolean {
	return autoRecConsumed;
}

async function choiceForAutoRec(): Promise<{ choice: RecordInputChoice; mode: RecordMode } | null> {
	const masterAvailable = masterMixUnavailableReason(rustMode.enabled) === null;
	const remembered = await getRememberedInput();
	const devices = masterAvailable ? null : await listRecorderDevices();
	const { mode, choice } = initialRecordSelection(remembered, devices, masterAvailable);
	if (choice !== null) return { choice, mode };
	if (masterAvailable) return { choice: { kind: 'master' }, mode: 'machine' };
	const fallback = defaultChoiceForMode('loopback', devices, false);
	return fallback === null ? null : { choice: fallback, mode: 'loopback' };
}

/** Fire-and-forget: never block play latency (SETREC-11802b). */
export function maybeStartAutoRecOnFirstPlay(): void {
	if (!autoRecEnabled || autoRecConsumed || !setRecordingEnabled()) return;
	autoRecConsumed = true;
	void (async () => {
		try {
			const status = await getRecorderStatus();
			if (status.active) return;
			const picked = await choiceForAutoRec();
			if (picked === null) return;
			const started = await startPerformanceRecorder(picked.choice, picked.mode);
			if (started.session_id === null || started.capture_source !== 'master') return;
			const capture = await import('./master-mix-capture');
			await capture.ensureMasterMixCapture(started.session_id);
		} catch (error) {
			console.warn('set auto-REC failed', error);
		}
	})();
}
