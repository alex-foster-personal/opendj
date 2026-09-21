/**
 * The browser-side Sentry scrub (OBS-06): a port of
 * apps/shared/telemetry/scrub.py, fail-closed.
 *
 * The loader SDK sends its own error events and replay breadcrumbs straight
 * to Sentry, so this module is the ONLY scrubber on that path. It mirrors
 * the engine's rules: token-shaped and path-shaped substrings out of every
 * string, a strict allowlist over the app-supplied sections (extra, tags,
 * breadcrumb data, non-SDK contexts), SDK-built context blocks kept in shape
 * with their string leaves scrubbed, request/user/server_name removed, frame
 * locals removed, and the SDK's DOM breadcrumb selectors stripped of the
 * attribute values that name library content. The two allowlists below are
 * generated from scrub.py by the build step in PR #3737; keep them identical
 * or `tests/unit/telemetry-consent.test.mjs` fails. Wired by
 * `telemetry-consent.ts` (`beforeSend`, `beforeBreadcrumb`,
 * `beforeAddRecordingEvent`).
 */

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

export interface ScrubbableEvent {
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
// One element of the SDK's selector path: `tag`, then any of `#id`, `.class`
// and `[name="value"]`. The SDK writes attribute values VERBATIM (no escaping),
// so a value holding `"`, or the ` > ` the path is joined with, cannot be told
// apart from the selector's own syntax. The parser below therefore accepts
// only what it can account for completely; anything else fails closed.
const SELECTOR_SEGMENT_RE = /^([a-zA-Z][\w-]*)((?:#[^\s#.[\]"]+|\.[^\s#.[\]"]+|\[[\w:-]+="[^"]*"\])*)$/;
const SELECTOR_PART_RE = /#[^\s#.[\]"]+|\.[^\s#.[\]"]+|\[([\w:-]+)="([^"]*)"\]/g;
const SELECTOR_JOIN = ' > ';

/**
 * `td.c-title[title="x"] > button[type="button"]` ->
 * `td.c-title[title="[filtered]"] > button[type="button"]`. A path that does
 * not parse end to end (a value containing a quote or the ` > ` joiner, a
 * fragment that is not `tag#id.class[attr="value"]...) becomes `[filtered]`
 * as a whole: the SDK does not escape values, so a partial rewrite could
 * leave the rest of the title in place (Codex on #3752).
 */
export function scrubSelector(text: string): string {
	const out: string[] = [];
	for (const segment of text.split(SELECTOR_JOIN)) {
		const m = SELECTOR_SEGMENT_RE.exec(segment);
		if (m === null) return FILTERED;
		const [, tag, parts] = m;
		let rebuilt = tag;
		let consumed = 0;
		for (const part of parts.matchAll(SELECTOR_PART_RE)) {
			if (part.index !== consumed) return FILTERED;
			consumed += part[0].length;
			const name = part[1];
			if (name === undefined) rebuilt += part[0];
			else if (SELECTOR_ATTRIBUTES_KEPT.has(name)) rebuilt += `[${name}="${part[2]}"]`;
			else rebuilt += `[${name}="${FILTERED}"]`;
		}
		if (consumed !== parts.length) return FILTERED;
		out.push(rebuilt);
	}
	return out.join(SELECTOR_JOIN);
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

/** The SDK's DOM breadcrumbs (`ui.click`, `ui.input`, ...): message is a selector path. */
function isDomBreadcrumb(category: unknown): boolean {
	return typeof category === 'string' && category.startsWith('ui.');
}

interface Breadcrumb {
	category?: unknown;
	message?: unknown;
	data?: unknown;
}

export function scrubBreadcrumb<T extends Breadcrumb>(crumb: T): T {
	if (typeof crumb.message === 'string') {
		crumb.message = isDomBreadcrumb(crumb.category)
			? scrubSelector(crumb.message)
			: scrubString(crumb.message);
	}
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
 * before it is buffered. DOM breadcrumb messages get the selector scrub,
 * other messages the string scrub, recorded node attributes the attribute
 * scrub; an event that cannot be scrubbed is dropped (null), the same
 * fail-closed answer as `scrubEvent`.
 */
export function scrubRecordingEvent<T extends RecordingEvent>(event: T): T | null {
	try {
		if (event.type !== 5 || !isRecord(event.data) || event.data.tag !== 'breadcrumb') return event;
		const payload = event.data.payload;
		if (!isRecord(payload)) return event;
		if (typeof payload.message === 'string') {
			payload.message = isDomBreadcrumb(payload.category)
				? scrubSelector(payload.message)
				: scrubString(payload.message);
		}
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
