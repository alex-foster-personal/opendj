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
/** Mirrors apps/shared/telemetry/scrub.py EXCEPTION_VALUE_WITHHELD. */
export const EXCEPTION_VALUE_WITHHELD = '[exception message withheld]';
/** Mirrors apps/shared/telemetry/scrub.py SAFE_EXCEPTION_TYPES (empty until audited). */
/** Mirrors apps/shared/telemetry/scrub.py BREADCRUMB_TEXT_WITHHELD. */
export const BREADCRUMB_TEXT_WITHHELD = '[breadcrumb text withheld]';
/** Mirrors scrub.py LOG_TEXT_WITHHELD: the browser cannot prove message provenance, so it never travels. */
export const LOG_TEXT_WITHHELD = '[log text withheld: template not proven a source literal]';
/** Mirrors scrub.py LOG_ARG_WITHHELD: a log parameter travels only as a plain number or boolean. */
export const LOG_ARG_WITHHELD = '<arg>';
/** Mirrors scrub.py _FRAME_FIELDS: source context, abs_path and locals never travel. */
const FRAME_FIELDS: ReadonlySet<string> = new Set([
	'filename', 'module', 'function', 'lineno', 'colno', 'in_app', 'platform'
]);
/** Mirrors scrub.py _EXCEPTION_FIELDS and _MECHANISM_FIELDS: anything else is free text and is dropped. */
const EXCEPTION_FIELDS: ReadonlySet<string> = new Set([
	'type', 'module', 'value', 'stacktrace', 'raw_stacktrace', 'mechanism', 'thread_id'
]);
const MECHANISM_FIELDS: ReadonlySet<string> = new Set([
	'type', 'handled', 'synthetic', 'exception_id', 'parent_id', 'is_exception_group', 'source'
]);
export const SAFE_EXCEPTION_TYPES: ReadonlySet<string> = new Set([]);
/**
 * Mirrors apps/shared/telemetry/__init__.py BROWSER_ERROR_NAMES: JavaScript's and
 * the DOM's own error types. Any other `Error.name` is set by page code and may
 * carry anything, so it travels only as BROWSER_ERROR_NAME_WITHHELD.
 */
export const BROWSER_ERROR_NAMES: ReadonlySet<string> = new Set([
	'Error', 'TypeError', 'RangeError', 'ReferenceError', 'SyntaxError', 'EvalError',
	'URIError', 'AggregateError', 'DOMException', 'AbortError', 'NotAllowedError',
	'NotFoundError', 'NotSupportedError', 'InvalidStateError', 'SecurityError',
	'QuotaExceededError', 'NetworkError', 'TimeoutError', 'EncodingError', 'DataError'
]);
/** Mirrors apps/shared/telemetry/__init__.py BROWSER_ERROR_NAME_WITHHELD. */
export const BROWSER_ERROR_NAME_WITHHELD = '[error name withheld]';

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

/** Each frame is rebuilt from FRAME_FIELDS, nothing else (scrub.py `_scrub_frames`). */
function scrubFrames(stacktrace: unknown): void {
	if (!isRecord(stacktrace)) return;
	const frames = Array.isArray(stacktrace.frames) ? stacktrace.frames : [];
	stacktrace.frames = frames.filter(isRecord).map((frame) => {
		const out: Record<string, unknown> = {};
		for (const key of Object.keys(frame)) if (FRAME_FIELDS.has(key)) out[key] = frame[key];
		if (typeof out.filename === 'string') out.filename = scrubString(out.filename);
		return out;
	});
}

function isPlainParam(value: unknown): boolean {
	return typeof value === 'number' || typeof value === 'boolean';
}

export interface ScrubbableEvent {
	message?: unknown;
	logentry?: unknown;
	transaction?: unknown;
	exception?: { values?: Array<{ type?: unknown; value?: unknown; stacktrace?: unknown; [key: string]: unknown }> };
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
// so once a value that can hold library content has started, nothing after
// it can be told apart from the selector's own syntax: a title of
// `A"] > span#PRIVATE[title="B` yields a path that parses cleanly with
// `#PRIVATE` in it (Codex on #3752). The scrub therefore keeps only the
// prefix BEFORE the first such value, which is DOM-derived by construction,
// and drops everything from that value on.
const SELECTOR_SEGMENT_RE = /^[a-zA-Z][\w-]*(?:#[^\s#.[\]"]+|\.[^\s#.[\]"]+|\[[\w:-]+="[^"]*"\])*$/;
const SELECTOR_ATTR_OPEN_RE = /\[([\w:-]+)="/g;
const SELECTOR_JOIN = ' > ';

function isWellFormedPath(text: string): boolean {
	return text.split(SELECTOR_JOIN).every((segment) => SELECTOR_SEGMENT_RE.test(segment));
}

/**
 * `main#app > td.c-title[title="x"] > span.title-text` ->
 * `main#app > td.c-title[title="[filtered]"]`.
 *
 * Everything up to the first attribute that is not in
 * `SELECTOR_ATTRIBUTES_KEPT` is kept, that attribute's value becomes
 * `[filtered]`, and the rest of the path is dropped: the SDK does not escape
 * values, so the text after that point may be the value itself, however it
 * happens to parse. The kept prefix is itself required to parse as a path
 * (tags, ids, classes, kept attributes); anything else, a stray fragment or
 * a message that is not a selector at all, becomes `[filtered]` whole.
 * Cost: the descendants of the first titled element are not named; the
 * rrweb snapshot still carries the click target by node id.
 */
export function scrubSelector(text: string): string {
	SELECTOR_ATTR_OPEN_RE.lastIndex = 0;
	let open: RegExpExecArray | null;
	while ((open = SELECTOR_ATTR_OPEN_RE.exec(text)) !== null) {
		const name = open[1];
		if (SELECTOR_ATTRIBUTES_KEPT.has(name)) continue;
		const prefix = text.slice(0, open.index);
		const lastSegment = prefix.split(SELECTOR_JOIN).pop() ?? '';
		// The prefix ends inside a segment (`td.c-title` before `[title="`);
		// it must be a well-formed path so far, with a tag on that segment.
		if (lastSegment.length === 0 || !isWellFormedPath(prefix)) return FILTERED;
		return `${prefix}[${name}="${FILTERED}"]`;
	}
	return isWellFormedPath(text) ? text : FILTERED;
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
			: BREADCRUMB_TEXT_WITHHELD;
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
 * scrub (non-DOM text is withheld: no loader breadcrumb proves its text
 * is code, so the fixed placeholder travels); an event that cannot be scrubbed is dropped (null), the same
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
				: BREADCRUMB_TEXT_WITHHELD;
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
		// No loader message carries provenance, so the fixed placeholders travel
		// (scrub.py `_scrub_message` / `_scrub_logentry` with nothing proven).
		if (typeof event.message === 'string') event.message = LOG_TEXT_WITHHELD;
		else if (isRecord(event.message)) event.message = { formatted: LOG_TEXT_WITHHELD };
		if (isRecord(event.logentry)) {
			const logentry = event.logentry;
			if ('message' in logentry || 'formatted' in logentry) logentry.message = LOG_TEXT_WITHHELD;
			delete logentry.formatted;
			if (Array.isArray(logentry.params))
				logentry.params = logentry.params.map((v) => (isPlainParam(v) ? v : LOG_ARG_WITHHELD));
			else if (logentry.params !== undefined && logentry.params !== null)
				logentry.params = LOG_ARG_WITHHELD;
		}
		if (typeof event.transaction === 'string') event.transaction = scrubString(event.transaction);
		for (const value of event.exception?.values ?? []) {
			const entry = value as Record<string, unknown>;
			for (const key of Object.keys(entry)) if (!EXCEPTION_FIELDS.has(key)) delete entry[key];
			if (isRecord(entry.mechanism)) {
				const mechanism = entry.mechanism;
				entry.mechanism = Object.fromEntries(
					Object.entries(mechanism).filter(([key]) => MECHANISM_FIELDS.has(key))
				);
			}
			if (entry.raw_stacktrace !== undefined) scrubFrames(entry.raw_stacktrace);
			const excType = typeof value.type === 'string' ? value.type : '';
			if (
				SAFE_EXCEPTION_TYPES.has(excType) &&
				typeof value.value === 'string'
			) {
				value.value = scrubString(value.value);
			} else if (value.value !== undefined && value.value !== null) {
				value.value = EXCEPTION_VALUE_WITHHELD;
			}
			// Error.name is page-chosen: only the platform's own names travel.
			if (value.type !== undefined && !BROWSER_ERROR_NAMES.has(excType))
				value.type = BROWSER_ERROR_NAME_WITHHELD;
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
