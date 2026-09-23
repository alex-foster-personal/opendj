/**
 * Test-user consent and Session Replay (OBS-05, OBS-06).
 *
 * The engine holds every Sentry send until the stored decision is
 * `accepted` for the current terms; this module is the page's half. It asks
 * `GET /api/v1/telemetry/consent` once the boot window has closed, raises the
 * dialog when the answer is `undecided` and there is something to consent
 * to, records the answer with `PUT`, and, after acceptance only, loads the
 * Sentry Loader Script from the URL the engine derived from the frontend
 * DSN. No `@sentry/*` package enters the bundle: the loader is an external
 * script and the CI bundle gate sums emitted chunks, not what a script tag
 * fetches at runtime.
 *
 * WHAT THE REPLAY RECORDS. The loader SDK is initialized here with
 * `maskAllText`, `maskAllInputs` and `blockAllMedia`, so track names on
 * screen become blocks and album art is not captured; canvases (waveforms)
 * are never recorded without a canvas integration, which is not added.
 * Error events the loader SDK captures on its own pass `scrubEvent`
 * (`telemetry-scrub.ts`, a port of the engine scrubber: tokens, paths,
 * section allowlists, fail-closed) and carry `origin=browser-sdk` so they are told apart from the
 * engine-forwarded copy.
 *
 * NEVER WHILE A DECK IS LIVE. The same rule as error reporting, enforced on
 * three paths: (1) a live-transport WATCHER (a rune effect on the deck
 * state, wired by app-init) stops the replay in the same microtask that
 * flips a deck to playing or audible, before any timer-driven flush can
 * run; (2) the stop DISCARDS the buffered tail (`forceFlush: false`) rather
 * than flushing it, so nothing recorded up to that instant leaves either;
 * (3) the loader SDK's own error capture returns null from `beforeSend`
 * while live, so a browser exception mid-set is dropped, not sent around
 * the engine's `any_deck_live` gate. A 2 s poll is the fallback for a page
 * without the watcher and restarts the replay after two idle ticks.
 */

import { api } from './api/client';
import type { BootScheduler } from './rb/boot-scheduler';
import {
	scrubBreadcrumb,
	scrubEvent,
	scrubRecordingEvent,
	type ScrubbableEvent
} from './telemetry-scrub';
import {
	currentConsent,
	setConsentDialogOpen,
	setCurrentConsent,
	type ConsentOut
} from './telemetry-consent-state';

export type ConsentDecision = 'accepted' | 'declined';

/** Seconds between live-transport polls while a replay is armed. */
export const REPLAY_LIVE_POLL_MS = 2000;
/** Idle polls before a stopped replay restarts: one covers a stop still ringing out. */
export const REPLAY_IDLE_TICKS_TO_RESTART = 2;

// ----- the loader SDK surface this module touches -----------------------------
interface LoaderReplay {
	start(): void;
	/** `forceFlush: false` discards the buffered segment instead of sending it. */
	stop(options?: { forceFlush?: boolean }): Promise<void> | void;
}

interface LoaderIntegration {
	name: string;
}

interface LoaderSentry {
	init(options: Record<string, unknown>): void;
	setTag(key: string, value: string): void;
	getReplay(): LoaderReplay | undefined;
	replayIntegration(options: Record<string, unknown>): LoaderIntegration;
}

/**
 * Default integrations the loader SDK would otherwise install and that send
 * envelopes of their own, outside the live gate. `BrowserSession` sends a
 * release-health `session` envelope at start, on every route change and on
 * every error: measured through the real loader (PR #3737), two of them left
 * mid-set after the replay had stopped. Release health is not used, so the
 * integration is removed rather than gated.
 */
export const DROPPED_DEFAULT_INTEGRATIONS: ReadonlySet<string> = new Set(['BrowserSession']);

/**
 * DOM attributes the replay recorder masks on every element. The SDK's own
 * default is title, placeholder and aria-label; the list is explicit here
 * because the UI carries library metadata in attributes (TrackTable's
 * `title={row.title}` and `title={row.artist}`, playlist and deck labels), and
 * an SDK default is not a contract. The real-loader acceptance spec decodes
 * a recorded segment and asserts a visible fixture title is absent from it.
 */
export const MASKED_ATTRIBUTES: readonly string[] = [
	'title',
	'placeholder',
	'aria-label',
	'aria-description',
	'aria-valuetext',
	'aria-roledescription',
	'alt',
	'data-title',
	'data-artist',
	'data-album',
	'data-name',
	'data-label',
	'data-tooltip'
];

/** The loader's `integrations` option: defaults minus the droppers, plus replay. */
export function selectIntegrations(
	defaults: LoaderIntegration[],
	replay: LoaderIntegration
): LoaderIntegration[] {
	return [...defaults.filter((i) => !DROPPED_DEFAULT_INTEGRATIONS.has(i.name)), replay];
}

declare global {
	interface Window {
		Sentry?: LoaderSentry;
		sentryOnLoad?: () => void;
	}
}

// ----- replay ------------------------------------------------------------------
export interface ReplayDeps {
	/** The page's live-transport read; `anyDeckPlaying` in production. */
	isLive: () => boolean;
	/** Script injection seam; production appends a script tag to <head>. */
	loadScript?: (url: string) => void;
	/** Interval seam; production is window.setInterval. Returns the cancel. */
	every?: (ms: number, fn: () => void) => () => void;
	/**
	 * Live-transport watcher: calls back the moment the live read changes,
	 * before any timer can run. Production passes `watchLiveTransport`
	 * (live-transport-watch.svelte.ts); a page without it falls back to the
	 * poll alone. Returns the unsubscribe.
	 */
	watchLive?: (onChange: (live: boolean) => void) => () => void;
}

let replayArmed = false;
let stopReplayPoll: (() => void) | null = null;
let stopLiveWatch: (() => void) | null = null;

function injectScript(url: string): void {
	if (typeof document === 'undefined') return;
	if (document.querySelector(`script[src="${url}"]`) !== null) return;
	const script = document.createElement('script');
	script.src = url;
	script.crossOrigin = 'anonymous';
	script.async = true;
	document.head.appendChild(script);
}

function everyMs(ms: number, fn: () => void): () => void {
	const id = setInterval(fn, ms);
	return () => clearInterval(id);
}

/**
 * Load the loader script and arm the live gate. Idempotent per page.
 *
 * `window.sentryOnLoad` runs before the loader's own init, and the options
 * it passes to `Sentry.init` are what the SDK starts with, so the masking
 * and sampling below are the effective configuration, not a suggestion.
 */
export function startReplay(consent: ConsentOut, deps: ReplayDeps): boolean {
	const url = consent.replay_loader_url;
	if (!url || replayArmed || typeof window === 'undefined') return false;
	replayArmed = true;
	const every = deps.every ?? everyMs;
	const loadScript = deps.loadScript ?? injectScript;
	let recording = true;
	let idleTicks = 0;
	// Stop and DISCARD: `forceFlush: false` drops the segment buffered since
	// the last periodic flush instead of sending it, so once a deck is live
	// nothing leaves, not even the seconds recorded up to this instant.
	const stopNow = (): void => {
		const replay = window.Sentry?.getReplay();
		if (replay === undefined || !recording) return;
		recording = false;
		idleTicks = 0;
		void replay.stop({ forceFlush: false });
	};

	window.sentryOnLoad = (): void => {
		const Sentry = window.Sentry;
		if (Sentry === undefined) return;
		Sentry.init({
			environment: consent.environment ?? undefined,
			release: consent.release ?? undefined,
			sendDefaultPii: false,
			// Client reports (counts of dropped events) are an envelope of their
			// own, sent on page-hide with no regard for the live gate; not used.
			sendClientReports: false,
			replaysSessionSampleRate: consent.replay_session_sample_rate,
			replaysOnErrorSampleRate: consent.replay_on_error_sample_rate,
			// A function, not an array: an array would ADD to the defaults and
			// keep BrowserSession (see DROPPED_DEFAULT_INTEGRATIONS).
			integrations: (defaults: LoaderIntegration[]) =>
				selectIntegrations(
					defaults,
					Sentry.replayIntegration({
						maskAllText: true,
						maskAllInputs: true,
						blockAllMedia: true,
						maskAttributes: [...MASKED_ATTRIBUTES],
						beforeAddRecordingEvent: scrubRecordingEvent
					})
				),
			// The loader SDK captures browser exceptions on its own; while a
			// deck is live they are dropped here, the same answer the engine
			// gives a forwarded error whose `any_deck_live` is true.
			beforeSend: (event: ScrubbableEvent) => (deps.isLive() ? null : scrubEvent(event)),
			beforeBreadcrumb: (crumb: { message?: unknown; data?: unknown }) => scrubBreadcrumb(crumb)
		});
		Sentry.setTag('origin', 'browser-sdk');
		// The watcher is the stop path that matters: it fires in the microtask
		// that flips a deck live, ahead of any flush timer. Recording may also
		// already be live at load time; the first poll tick covers that.
		if (deps.watchLive !== undefined) {
			stopLiveWatch = deps.watchLive((live) => {
				if (live) stopNow();
			});
		}
		stopReplayPoll = every(REPLAY_LIVE_POLL_MS, () => {
			const replay = window.Sentry?.getReplay();
			if (replay === undefined) return;
			if (deps.isLive()) {
				stopNow();
				return;
			}
			if (recording) return;
			idleTicks += 1;
			if (idleTicks >= REPLAY_IDLE_TICKS_TO_RESTART) {
				recording = true;
				replay.start();
			}
		});
	};
	loadScript(url);
	return true;
}

// ----- boot and answer ------------------------------------------------------------
export interface BootDeps extends ReplayDeps {
	scheduler?: BootScheduler;
	fetchConsent?: () => Promise<ConsentOut>;
	/** Dialog seam: production mounts the overlay into <body>; tests pass a no-op. */
	showDialog?: () => Promise<() => void>;
}

let bootDeps: BootDeps | null = null;
let unmountDialog: (() => void) | null = null;

/**
 * Mount the dialog component on demand. It is imported here, not in the
 * root layout, so the component is fetched only for a tester who has not
 * answered yet and never joins the first-paint bundle. The answer function
 * goes in as a prop: the overlay reads its state from
 * telemetry-consent-state and never imports this module, which would close
 * an import cycle with the dynamic import above.
 */
async function mountDialog(): Promise<() => void> {
	const [{ mount, unmount }, { default: Overlay }] = await Promise.all([
		import('svelte'),
		import('$lib/components/telemetry/TelemetryConsentOverlay.svelte')
	]);
	const instance = mount(Overlay, {
		target: document.body,
		props: { answer: (decision: ConsentDecision) => answerConsent(decision) }
	});
	return () => void unmount(instance);
}

async function fetchConsent(): Promise<ConsentOut> {
	const { data } = await api.GET('/api/v1/telemetry/consent');
	if (data === undefined) throw new Error('telemetry consent: empty response from the engine');
	return data;
}

/**
 * Ask only when an answer would change anything: consent must be REQUIRED
 * on this boot (an operator's explicit OPENDJ_TELEMETRY=1 is never held, so
 * a dialog there would offer a decline that cannot close the gate), the
 * decision must be open, and an acceptance must turn something on.
 */
export function shouldAsk(consent: ConsentOut): boolean {
	if (!consent.consent_required) return false;
	if (consent.decision !== 'undecided') return false;
	return consent.telemetry_active || consent.replay_loader_url !== null;
}

/**
 * Page-lifetime start: fetch the decision after the boot window, raise the
 * dialog or arm replay. Returns the teardown. Fetch failures are left to
 * the client's own error path (`ApiError` is thrown, reported, and the app
 * simply does not ask this session).
 */
export function bootTelemetryConsent(deps: BootDeps): () => void {
	bootDeps = deps;
	const fetcher = deps.fetchConsent ?? fetchConsent;
	const run = async (): Promise<void> => {
		const consent = await fetcher();
		if (bootDeps !== deps) return; // torn down while the request was in flight
		setCurrentConsent(consent);
		if (consent.decision === 'accepted') {
			// A stored acceptance does not outrank THIS boot's decision: with the
			// opt-out marker or OPENDJ_TELEMETRY=0 the engine has no client, the
			// route withholds the loader URL, and nothing is loaded here either.
			if (consent.telemetry_active) startReplay(consent, deps);
			return;
		}
		if (!shouldAsk(consent)) return;
		unmountDialog = await (deps.showDialog ?? mountDialog)();
		if (bootDeps !== deps) return;
		setConsentDialogOpen(true);
	};
	// app-init already runs this inside a deferred boot task (and imports this
	// module there), so the default is to fetch now; a caller that has not
	// yielded to the boot window passes its scheduler and waits its turn.
	if (deps.scheduler !== undefined) deps.scheduler.defer('telemetry-consent:fetch', run);
	else void run();
	return () => {
		setConsentDialogOpen(false);
		unmountDialog?.();
		unmountDialog = null;
		setCurrentConsent(null);
		stopReplayPoll?.();
		stopReplayPoll = null;
		stopLiveWatch?.();
		stopLiveWatch = null;
		replayArmed = false;
		bootDeps = null;
		if (typeof window !== 'undefined') delete window.sentryOnLoad;
	};
}

/** The dialog's buttons. Records the answer, then arms replay on acceptance. */
export async function answerConsent(
	decision: ConsentDecision,
	put: (body: { decision: ConsentDecision; terms_version: string }) => Promise<ConsentOut> = putConsent
): Promise<ConsentOut> {
	const terms_version = currentConsent()?.terms_current_version;
	if (!terms_version) throw new Error('telemetry consent: no terms version loaded');
	const consent = await put({ decision, terms_version });
	setCurrentConsent(consent);
	setConsentDialogOpen(false);
	if (consent.decision === 'accepted' && consent.telemetry_active && bootDeps !== null)
		startReplay(consent, bootDeps);
	return consent;
}

async function putConsent(body: {
	decision: ConsentDecision;
	terms_version: string;
}): Promise<ConsentOut> {
	const { data } = await api.PUT('/api/v1/telemetry/consent', { body });
	if (data === undefined) throw new Error('telemetry consent: empty response from the engine');
	return data;
}
