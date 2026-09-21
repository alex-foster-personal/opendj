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
 * Error events the loader SDK captures on its own pass `scrubEvent`, a port
 * of the engine scrubber's path rule, and carry `origin=browser-sdk` so
 * they are told apart from the engine-forwarded copy.
 *
 * NEVER WHILE A DECK IS LIVE. The same rule as error reporting: a poll reads
 * the live-transport probe and stops the replay while any deck is playing or
 * audible, restarting it after two idle ticks. `stop()` flushes the segment
 * recorded so far, so the one send a mix can trigger is at its very start,
 * never during it.
 */

import { api } from './api/client';
import type { BootScheduler } from './rb/boot-scheduler';
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
	stop(): Promise<void> | void;
}

interface LoaderSentry {
	init(options: Record<string, unknown>): void;
	setTag(key: string, value: string): void;
	getReplay(): LoaderReplay | undefined;
	replayIntegration(options: Record<string, unknown>): unknown;
}

declare global {
	interface Window {
		Sentry?: LoaderSentry;
		sentryOnLoad?: () => void;
	}
}

// ----- the scrub (a port of apps/shared/telemetry/scrub.py's path rule) --------
const FS_ROOT =
	'(?:\\b[A-Za-z]:[\\\\/]|~[\\\\/]|/(?:Users|home|Volumes|mnt|media|private|var|tmp|opt|srv|root|Library|System|Applications)[\\\\/])';
const AUDIO_EXT = 'mp3|flac|wav|aiff|aif|m4a|ogg|aac|opus|alac|wma|aax';
const PATH_RE = new RegExp(
	`${FS_ROOT}[^\\r\\n"'<>|?*]*?\\.[A-Za-z0-9]{1,8}\\b|${FS_ROOT}[^\\s"'<>|?*\\r\\n]*|[^\\s"'<>|?*\\r\\n/\\\\]+\\.(?:${AUDIO_EXT})\\b`,
	'gi'
);
const EXT_RE = /(\.[A-Za-z0-9]{1,8})$/;

export function redactPaths(text: string): string {
	return text.replace(PATH_RE, (match) => {
		const ext = EXT_RE.exec(match);
		return ext ? `<path${ext[1]}>` : '<path>';
	});
}

interface ScrubbableEvent {
	message?: unknown;
	exception?: { values?: Array<{ value?: unknown }> };
	breadcrumbs?: Array<{ message?: unknown; data?: unknown }>;
	request?: unknown;
	user?: unknown;
	tags?: Record<string, unknown>;
}

/** Path-scrub the free text of a loader-SDK event; drop request and user. */
export function scrubEvent<T extends ScrubbableEvent>(event: T): T {
	if (typeof event.message === 'string') event.message = redactPaths(event.message);
	for (const value of event.exception?.values ?? []) {
		if (typeof value.value === 'string') value.value = redactPaths(value.value);
	}
	for (const crumb of event.breadcrumbs ?? []) {
		if (typeof crumb.message === 'string') crumb.message = redactPaths(crumb.message);
		delete crumb.data;
	}
	delete event.request;
	delete event.user;
	event.tags = { ...(event.tags ?? {}), origin: 'browser-sdk' };
	return event;
}

// ----- replay ------------------------------------------------------------------
export interface ReplayDeps {
	/** The page's live-transport read; `anyDeckPlaying` in production. */
	isLive: () => boolean;
	/** Script injection seam; production appends a script tag to <head>. */
	loadScript?: (url: string) => void;
	/** Interval seam; production is window.setInterval. Returns the cancel. */
	every?: (ms: number, fn: () => void) => () => void;
}

let replayArmed = false;
let stopReplayPoll: (() => void) | null = null;

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

	window.sentryOnLoad = (): void => {
		const Sentry = window.Sentry;
		if (Sentry === undefined) return;
		Sentry.init({
			environment: consent.environment ?? undefined,
			release: consent.release ?? undefined,
			sendDefaultPii: false,
			replaysSessionSampleRate: consent.replay_session_sample_rate,
			replaysOnErrorSampleRate: consent.replay_on_error_sample_rate,
			integrations: [
				Sentry.replayIntegration({
					maskAllText: true,
					maskAllInputs: true,
					blockAllMedia: true
				})
			],
			beforeSend: (event: ScrubbableEvent) => scrubEvent(event),
			beforeBreadcrumb: (crumb: { message?: unknown; data?: unknown }) => {
				if (typeof crumb.message === 'string') crumb.message = redactPaths(crumb.message);
				delete crumb.data;
				return crumb;
			}
		});
		Sentry.setTag('origin', 'browser-sdk');
		// Recording may already have begun by the time the first poll runs;
		// a set that is live at load time stops it on that first tick.
		stopReplayPoll = every(REPLAY_LIVE_POLL_MS, () => {
			const replay = window.Sentry?.getReplay();
			if (replay === undefined) return;
			if (deps.isLive()) {
				idleTicks = 0;
				if (recording) {
					recording = false;
					void replay.stop();
				}
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

/** Ask only when there is something an acceptance would turn on. */
export function shouldAsk(consent: ConsentOut): boolean {
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
			startReplay(consent, deps);
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
	if (consent.decision === 'accepted' && bootDeps !== null) startReplay(consent, bootDeps);
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
