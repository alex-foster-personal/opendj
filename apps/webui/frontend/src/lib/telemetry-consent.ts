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
 * of the engine scrubber (tokens, paths, section allowlists, fail-closed),
 * and carry `origin=browser-sdk` so they are told apart from the
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

// ----- the scrub (a port of apps/shared/telemetry/scrub.py, fail-closed) ------
// The loader SDK sends its own error events straight to Sentry, so this is
// the ONLY scrubber on that path. It mirrors the engine's rules: token-shaped
// and path-shaped substrings out of every string, a strict allowlist over the
// app-supplied sections (extra, tags, breadcrumb data, non-SDK contexts),
// SDK-built context blocks kept in shape with their string leaves scrubbed,
// request/user/server_name removed, frame locals removed. The two allowlists
// below are generated from scrub.py by the build step in PR #3737; keep them
// identical or `tests/unit/telemetry-consent.test.mjs` fails.
const FS_ROOT =
	'(?:\\b[A-Za-z]:[\\\\/]|~[\\\\/]|/(?:Users|home|Volumes|mnt|media|private|var|tmp|opt|srv|root|Library|System|Applications)[\\\\/])';
const AUDIO_EXT = 'mp3|flac|wav|aiff|aif|m4a|ogg|aac|opus|alac|wma|aax';
const PATH_RE = new RegExp(
	`${FS_ROOT}[^\\r\\n"'<>|?*]*?\\.[A-Za-z0-9]{1,8}\\b|${FS_ROOT}[^\\s"'<>|?*\\r\\n]*|[^\\s"'<>|?*\\r\\n/\\\\]+\\.(?:${AUDIO_EXT})\\b`,
	'gi'
);
const EXT_RE = /(\.[A-Za-z0-9]{1,8})$/;
const TOKENISH_RE =
	/(?:bearer\s+)[a-z0-9._\-+=/]{8,}|sk-[a-z0-9]{10,}|ghp_[a-z0-9]{10,}|xox[baprs]-[a-z0-9-]{10,}|(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|passwd|authorization)\s*[:=]\s*['"]?[^\s'"]{8,}/gi;
export const FILTERED = '[filtered]';
export const REDACTED = '[redacted]';

/** Context keys that may travel verbatim (scrub.py ALLOWED_CONTEXT_KEYS). */
export const ALLOWED_CONTEXT_KEYS: ReadonlySet<string> = new Set([
	'adapter',
	'any_deck_live',
	'attempt',
	'audio_worklet_available',
	'boot_id',
	'build_sha',
	'build_source',
	'channels',
	'client_event_id',
	'contract_rev',
	'count',
	'deck_id',
	'duration_ms',
	'engine_version',
	'error_code',
	'error_id',
	'fallback_message',
	'host',
	'http_status',
	'job_id',
	'job_kind',
	'kind',
	'lane_label',
	'method',
	'origin',
	'platform',
	'python_version',
	'route',
	'sample_rate',
	'secure_context',
	'source',
	'source_site',
	'status',
	'url',
	'user_agent'
]);
/** SDK-built blocks that keep their shape, string leaves scrubbed (scrub.py SDK_CONTEXT_BLOCKS). */
export const SDK_CONTEXT_BLOCKS: ReadonlySet<string> = new Set([
	'app',
	'browser',
	'cloud_resource',
	'culture',
	'device',
	'gpu',
	'missing_instrumentation',
	'os',
	'profile',
	'replay',
	'response',
	'runtime',
	'trace'
]);

export function redactPaths(text: string): string {
	return text.replace(PATH_RE, (match) => {
		const ext = EXT_RE.exec(match);
		return ext ? `<path${ext[1]}>` : '<path>';
	});
}

/** Token-shaped and path-shaped substrings out of any free text. */
export function scrubString(text: string): string {
	return redactPaths(text.replace(TOKENISH_RE, FILTERED));
}

type Json = unknown;

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Keep allowlisted keys, redact the rest, scrub what survives. */
function allowlist(value: Json): Json {
	if (Array.isArray(value)) return value.map(allowlist);
	if (isRecord(value)) {
		const out: Record<string, unknown> = {};
		for (const [key, item] of Object.entries(value)) {
			out[key] = ALLOWED_CONTEXT_KEYS.has(key.toLowerCase()) ? allowlist(item) : REDACTED;
		}
		return out;
	}
	if (typeof value === 'string') return scrubString(value);
	return value;
}

/** Scrub string leaves, keep every key and the shape around them. */
function scrubValues(value: Json): Json {
	if (Array.isArray(value)) return value.map(scrubValues);
	if (isRecord(value)) {
		const out: Record<string, unknown> = {};
		for (const [key, item] of Object.entries(value)) out[key] = scrubValues(item);
		return out;
	}
	if (typeof value === 'string') return scrubString(value);
	return value;
}

function scrubContexts(contexts: Json): Json {
	if (!isRecord(contexts)) return allowlist(contexts);
	const out: Record<string, unknown> = {};
	for (const [name, block] of Object.entries(contexts)) {
		out[name] = SDK_CONTEXT_BLOCKS.has(name.toLowerCase()) ? scrubValues(block) : allowlist(block);
	}
	return out;
}

interface Frame {
	filename?: unknown;
	abs_path?: unknown;
	vars?: unknown;
	[key: string]: unknown;
}

function scrubFrames(stacktrace: unknown): void {
	if (!isRecord(stacktrace) || !Array.isArray(stacktrace.frames)) return;
	for (const frame of stacktrace.frames as Frame[]) {
		if (!isRecord(frame)) continue;
		delete frame.vars;
		if (typeof frame.filename === 'string') frame.filename = scrubString(frame.filename);
		if (typeof frame.abs_path === 'string') frame.abs_path = scrubString(frame.abs_path);
	}
}

interface ScrubbableEvent {
	message?: unknown;
	logentry?: unknown;
	transaction?: unknown;
	exception?: { values?: Array<{ value?: unknown; stacktrace?: unknown }> };
	breadcrumbs?: unknown;
	request?: unknown;
	user?: unknown;
	server_name?: unknown;
	extra?: unknown;
	contexts?: unknown;
	tags?: Record<string, unknown>;
}

/**
 * Attribute values the SDK writes into a click/input breadcrumb's CSS-selector
 * `message` (`td.c-title[title="<track>"]`) and into the recorded node's
 * `attributes`. rrweb masking covers the DOM snapshot, not these: the SDK
 * builds the selector from the live element, always including `aria-label`,
 * `name`, `title` and `alt`, so a masked replay still carried the visible
 * track title in its `ui.click` breadcrumb (real-loader e2e, Mon 21 Sep 2026).
 * Only structural, content-free attributes keep their value.
 */
export const SELECTOR_ATTRIBUTES_KEPT: ReadonlySet<string> = new Set([
	'type',
	'role',
	'data-testid',
	'data-test-id',
	'data-sentry-component',
	'data-sentry-element',
	'disabled',
	'aria-disabled'
]);
const SELECTOR_ATTR_RE = /\[([\w:-]+)="([^"]*)"\]/g;

/** `a.b[title="x"][type="button"]` -> `a.b[title="[filtered]"][type="button"]`. */
export function scrubSelector(text: string): string {
	return text.replace(SELECTOR_ATTR_RE, (_m, name: string, value: string) =>
		SELECTOR_ATTRIBUTES_KEPT.has(name) ? `[${name}="${value}"]` : `[${name}="${FILTERED}"]`
	);
}

function scrubNodeAttributes(node: unknown): void {
	if (!isRecord(node) || !isRecord(node.attributes)) return;
	for (const key of Object.keys(node.attributes)) {
		// The SDK renames data-testid to testId before it lands here.
		if (key === 'testId' || key === 'id' || key === 'class' || SELECTOR_ATTRIBUTES_KEPT.has(key))
			continue;
		node.attributes[key] = FILTERED;
	}
}

export function scrubBreadcrumb<T extends { message?: unknown; data?: unknown }>(crumb: T): T {
	if (typeof crumb.message === 'string') crumb.message = scrubSelector(scrubString(crumb.message));
	if (crumb.data !== undefined && crumb.data !== null) crumb.data = allowlist(crumb.data);
	return crumb;
}

interface RecordingEvent {
	type: number;
	data?: unknown;
}

/**
 * `replayIntegration({ beforeAddRecordingEvent })`: every custom rrweb event
 * (type 5: the replay's own breadcrumbs and performance spans) passes here
 * before it is buffered. Breadcrumb messages and recorded node attributes get
 * the selector scrub; an event that cannot be scrubbed is dropped (null), the
 * same fail-closed answer as `scrubEvent`.
 */
export function scrubRecordingEvent<T extends RecordingEvent>(event: T): T | null {
	try {
		if (event.type !== 5 || !isRecord(event.data) || event.data.tag !== 'breadcrumb') return event;
		const payload = event.data.payload;
		if (!isRecord(payload)) return event;
		if (typeof payload.message === 'string')
			payload.message = scrubSelector(scrubString(payload.message));
		if (isRecord(payload.data)) scrubNodeAttributes(payload.data.node);
		return event;
	} catch {
		return null;
	}
}

/**
 * Strip library content and secrets from a loader-SDK event. Returns null,
 * i.e. drops the event, when scrubbing throws: an event whose contents are
 * unknown is not sent (the engine scrubber's rule, scrub.py `scrub_event`).
 */
export function scrubEvent<T extends ScrubbableEvent>(event: T): T | null {
	try {
		if (typeof event.message === 'string') event.message = scrubString(event.message);
		else if (isRecord(event.message) && typeof event.message.formatted === 'string')
			event.message.formatted = scrubString(event.message.formatted);
		if (isRecord(event.logentry)) {
			for (const key of ['message', 'formatted']) {
				if (typeof event.logentry[key] === 'string')
					event.logentry[key] = scrubString(event.logentry[key] as string);
			}
			if (Array.isArray(event.logentry.params))
				event.logentry.params = event.logentry.params.map((v) =>
					typeof v === 'string' ? scrubString(v) : v
				);
		}
		if (typeof event.transaction === 'string') event.transaction = scrubString(event.transaction);
		for (const value of event.exception?.values ?? []) {
			if (typeof value.value === 'string') value.value = scrubString(value.value);
			scrubFrames(value.stacktrace);
		}
		const crumbs = isRecord(event.breadcrumbs) ? event.breadcrumbs.values : event.breadcrumbs;
		if (Array.isArray(crumbs)) for (const crumb of crumbs) if (isRecord(crumb)) scrubBreadcrumb(crumb);
		if (event.extra !== undefined && event.extra !== null) event.extra = allowlist(event.extra);
		if (event.contexts !== undefined && event.contexts !== null)
			event.contexts = scrubContexts(event.contexts);
		const tags = isRecord(event.tags) ? (allowlist(event.tags) as Record<string, unknown>) : {};
		event.tags = { ...tags, origin: 'browser-sdk' };
		delete event.request;
		delete event.user;
		delete event.server_name;
		return event;
	} catch {
		return null;
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
