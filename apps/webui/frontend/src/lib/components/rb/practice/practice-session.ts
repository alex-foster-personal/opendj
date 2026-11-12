/**
 * Practice route command session (ADR-0137). Replica controls dispatch through the same
 * action glue as a controller, and the glue's mixer, transport and deck commands go through
 * dispatchPerformanceCommand, which refuses with no active command session. /performance and
 * Trackify start theirs with installPerformanceBrowserIpc; /practice does the same, claiming the
 * performance mount first so a retiring page's teardown sees a newer owner.
 *
 * Requirements (mini-PRD):
 *   ✔︎ installPracticeSession starts a command session and returns its uninstall.
 *     [if] a fader move on /practice is refused for want of a command session [then] ⛔️
 *     [if] uninstall leaves the session active after the route unmounts [then] ⛔️
 *     [if] leaving /practice leaves the engine it took over playing [then] ⛔️
 *     [if] leaving /practice for /performance disposes the engine the new mount owns [then] ⛔️
 *   ✔︎ Drill hardware hits (#10251): a real-controller press from the drilled device that
 *     resolves by panel wire to a control feeds onPress; read-only, sends no MIDI, and the
 *     engine is reached only by dynamic import.
 *     [if] a note-on from another device or a note-off counts as a press [then] ⛔️
 *     [if] one knob twist counts as more than one press [then] ⛔️
 */

import { noteGigRuntimeMounted } from '$lib/rb/library-mode-runtime';
import {
	claimPerformanceMount,
	currentPerformanceMount,
	installPerformanceBrowserIpc
} from '$lib/rb/performance-ipc.svelte';
import { createPerformanceRouteTeardown } from '$lib/rb/performance-route-teardown';
import { controlIdForWire, type PanelLayout } from './practice-bindings';

import type { DeviceMap, MidiInputMessage } from '$lib/rb/midi/midi-types';

export function installPracticeSession(): () => void {
	// A fresh gig generation, or releaseGigRuntime skips the dispose of a second practice visit.
	noteGigRuntimeMounted();
	const mountGeneration = claimPerformanceMount();
	const beginTeardown = createPerformanceRouteTeardown();
	const uninstallIpc = installPerformanceBrowserIpc();
	return () => {
		try {
			uninstallIpc();
		} finally {
			// A /performance remount owns the engine now: leave it playing.
			if (currentPerformanceMount() === mountGeneration) void beginTeardown();
		}
	};
}

type MidiInputSubscribe = (listener: (message: MidiInputMessage) => void) => () => void;

/** Feed presses from `map`'s device to onPress: note-on only, and one press per cc gesture. */
export function watchDrillHardwareHits(
	layout: PanelLayout,
	map: DeviceMap,
	subscribe: MidiInputSubscribe,
	onPress: (controlId: string) => void
): () => void {
	let lastCcControlId: string | null = null;
	return subscribe((message) => {
		if (message.mapNameMatch !== map.nameMatch || message.decoded === null) return;
		const { kind } = message.decoded;
		if (kind !== 'note' && kind !== 'cc') return;
		const controlId = controlIdForWire(layout, message.decoded);
		if (controlId === null) return;
		if (kind === 'note') {
			if ((message.status & 0xf0) !== 0x90 || message.data2 === 0) return;
			lastCcControlId = null;
		} else if (kind === 'cc') {
			if (controlId === lastCcControlId) return;
			lastCcControlId = controlId;
		}
		onPress(controlId);
	});
}

/** Route entry: lazily loads the MIDI engine and wires watchDrillHardwareHits. Starts no MIDI. */
export function installDrillHardwareHits(
	layout: PanelLayout,
	map: DeviceMap,
	onPress: (controlId: string) => void
): () => void {
	let unsubscribe: (() => void) | null = null;
	let cancelled = false;
	import('$lib/rb/midi/midi-engine').then(
		(engine) => {
			if (!cancelled)
				unsubscribe = watchDrillHardwareHits(layout, map, engine.subscribeMidiInput, onPress);
		},
		(err: unknown) => console.error('[practice] MIDI engine failed to load for drill hits', err)
	);
	return () => {
		cancelled = true;
		unsubscribe?.();
	};
}
