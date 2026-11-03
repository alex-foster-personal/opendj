/**
 * Browser harness for silence-expected-audible.spec.ts (Codex P1 #6636 r4283616386).
 *
 * Runs each scenario through the shipped stack with production HTTP only:
 *   startAppInstruments → readSilenceSourceDeckSnaps → audio-engine graph → noteMasterSilence
 *
 * One module so master-silence-report and app-init share a single reader registration.
 */
import { startAppInstruments } from '$lib/rb/app-init';
import { deckMixBuffer, deckStates, engine, peekMasterMeterReading, upgradeDeckStemsForTest } from '$lib/rb/audio-engine.svelte';
import {
	disconnectDeckProcessorForTest,
	sampleMasterSilenceForSilenceE2e
} from '$lib/rb/audio-engine-silence-probes';
import type { SilenceDropoutPlan } from '$lib/rb/silence-dropout';
import { executeSilenceDropoutPlan } from '$lib/rb/silence-dropout-act';
import {
	masterSilenceState,
	resetMasterSilenceWatch,
	setSilenceDropoutHandler
} from '$lib/rb/master-silence-report';
import { SILENCE_RMS_FLOOR, SILENT_WHILE_PLAYING_MS } from '$lib/rb/silence-watchdog';
type DeckId = 1 | 2 | 3 | 4;

const TEST_DECK: DeckId = 1;
export const MIXTURE_STABLE_ID = 'a'.repeat(40);
export const SILENT_INTRO_STABLE_ID = 'b'.repeat(40);

export interface Scenario {
	path: 'mix' | 'stems';
	track: 'mixture' | 'silentIntro';
	position_sec: number;
	fader?: number;
	cue?: boolean;
	mute?: Array<'vocal' | 'instrumental'>;
	solo?: 'vocal' | 'instrumental';
	dropout?: boolean;
	expectOpenSilent?: boolean;
}

export interface Outcome {
	open_rms: number;
	held_rms_max: number;
	samples: number;
	plans: Array<{ stop_decks: number[]; toast: string }>;
	/** The test deck's transport once the production dropout handler has run (Codex P1 r4291679015). */
	deck_playing_after: boolean;
	errors: string[];
}

function immediateBootScheduler() {
	return {
		defer: (_label: string, task: () => void | Promise<void>) => {
			void task();
		},
		deckLoadStarted: () => () => {},
		listingWalkStarted: () => () => {},
		start: () => () => {}
	};
}

let stopInstruments: (() => void) | null = null;

function ensureAppInstruments(): void {
	if (stopInstruments !== null) return;
	stopInstruments = startAppInstruments(immediateBootScheduler());
}

function masterRmsFromReport(): number {
	const rms = masterSilenceState().rms;
	return rms === null || !Number.isFinite(rms) ? 0 : rms;
}

/** Open-graph proof: stem graphs can take a beat before the master tap shows level. */
function masterOpenLevel(): number {
	sampleMasterSilenceForSilenceE2e();
	const reported = masterRmsFromReport();
	const normalized = peekMasterMeterReading().normalized;
	const meter = Number.isFinite(normalized) ? normalized : 0;
	return Math.max(reported, meter);
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function stableIdForTrack(track: Scenario['track']): string {
	return track === 'silentIntro' ? SILENT_INTRO_STABLE_ID : MIXTURE_STABLE_ID;
}

export async function runScenario(scenario: Scenario): Promise<Outcome> {
	ensureAppInstruments();
	const errors: string[] = [];
	const plans: Array<{ stop_decks: number[]; toast: string }> = [];
	const executions: Promise<void>[] = [];
	// Record the plan, then run the SHIPPED action path on it (engine stop and
	// AutoPlay recovery), so the outcome reports what the handler did to the deck.
	setSilenceDropoutHandler((plan: SilenceDropoutPlan) => {
		plans.push({ stop_decks: [...plan.stop_decks], toast: plan.toast });
		executions.push(executeSilenceDropoutPlan(plan));
	});
	resetMasterSilenceWatch();
	try {
		await engine.dispose();
		const stableId = stableIdForTrack(scenario.track);
		await engine.load(TEST_DECK, stableId, { stems: false });
		if (scenario.path === 'stems') {
			const mix = deckMixBuffer(TEST_DECK);
			if (mix === null) throw new Error('mix buffer missing after load');
			await upgradeDeckStemsForTest(TEST_DECK, stableId, mix);
			if (deckStates[TEST_DECK].stems.status !== 'ready') {
				throw new Error(`stems not ready: ${deckStates[TEST_DECK].stems.status} ${deckStates[TEST_DECK].stems.error ?? ''}`);
			}
		}
		await engine.quantizedSeek(TEST_DECK, scenario.position_sec * 1000, true);
		await engine.play(TEST_DECK);
		const liveDeadline = performance.now() + 15_000;
		while (performance.now() < liveDeadline) {
			const st = deckStates[TEST_DECK];
			if (st.playing && st.audible) break;
			await sleep(50);
		}
		if (!deckStates[TEST_DECK].playing) {
			throw new Error('deck never entered playing state');
		}
		const audibleFloor = SILENCE_RMS_FLOOR * 10;
		let open_rms = 0;
		const openDeadline = performance.now() + (scenario.path === 'stems' ? 20_000 : 8_000);
		while (performance.now() < openDeadline) {
			await sleep(50);
			open_rms = Math.max(open_rms, masterOpenLevel());
			if (!scenario.expectOpenSilent && open_rms >= audibleFloor) break;
			if (scenario.expectOpenSilent) break;
		}
		if (scenario.fader !== undefined) engine.setFader(TEST_DECK, scenario.fader);
		if (scenario.cue !== undefined) engine.setChannelCue(TEST_DECK, scenario.cue);
		for (const stem of scenario.mute ?? []) engine.setStemMute(TEST_DECK, stem, true);
		if (scenario.solo !== undefined) engine.setStemSolo(TEST_DECK, scenario.solo, true);
		if (scenario.dropout) disconnectDeckProcessorForTest(TEST_DECK);
		await sleep(150);
		let held_rms_max = 0;
		let samples = 0;
		const until = performance.now() + SILENT_WHILE_PLAYING_MS + 300;
		while (performance.now() < until) {
			sampleMasterSilenceForSilenceE2e();
			held_rms_max = Math.max(held_rms_max, masterRmsFromReport());
			samples += 1;
			await sleep(100);
		}
		await Promise.all(executions);
		return { open_rms, held_rms_max, samples, plans, deck_playing_after: deckStates[TEST_DECK].playing, errors };
	} catch (error) {
		errors.push(String(error));
		return { open_rms: 0, held_rms_max: 0, samples: 0, plans, deck_playing_after: deckStates[TEST_DECK].playing, errors };
	} finally {
		resetMasterSilenceWatch();
		setSilenceDropoutHandler(null);
		try {
			await engine.dispose();
		} catch {
			// A failed load may leave the graph half-built; the next scenario starts clean.
		}
	}
}

declare global {
	interface Window {
		__silenceExpectedAudibleHarness?: {
			runScenario: typeof runScenario;
			MIXTURE_STABLE_ID: string;
			SILENT_INTRO_STABLE_ID: string;
		};
	}
}

if (typeof window !== 'undefined') {
	window.__silenceExpectedAudibleHarness = {
		runScenario,
		MIXTURE_STABLE_ID,
		SILENT_INTRO_STABLE_ID
	};
}
