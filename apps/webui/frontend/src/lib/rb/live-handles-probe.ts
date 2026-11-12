/**
 * Live handles probe (PERF-LEAK-11789): an opt-in in-page registry of what the
 * page holds open, armed only by the PERFPROBE-01 switch (`?perfprobe=1` or
 * localStorage `mdt.perfProbe=1`) and loaded lazily from app-init, so it
 * costs nothing when off.
 *
 * It wraps setInterval/clearInterval, setTimeout/clearTimeout (timeouts over
 * 5 s only), addEventListener/removeEventListener on window and document
 * (browser identity rule: type, listener and capture flag), and
 * requestAnimationFrame (bucketed by callback name). Each entry is charged to
 * its creator: the first non-probe frame of a captured stack, trimmed to a
 * repo-relative `path:line`.
 *
 * Read it in DevTools as `window.__mdtLiveHandles()`.
 *
 * Known limit: handles created before the probe arms (module-scope intervals
 * and listeners in stores evaluated at import time) are not seen, and
 * clearing such a handle leaves the counts unchanged.
 */

export const LONG_TIMEOUT_MS = 5_000;

export interface LiveHandlesTotals {
	intervals: number;
	long_timeouts: number;
	/** Listeners on window and document. */
	window_listeners: number;
	/** Pending requestAnimationFrame callbacks. */
	rafs: number;
}

export interface LiveHandlesSnapshot extends LiveHandlesTotals {
	by_site: Record<string, number>;
}

export interface LiveHandlesProbe {
	snapshot(): LiveHandlesSnapshot;
	totals(): LiveHandlesTotals;
	uninstall(): void;
}

export type LiveHandlesWindow = { __mdtLiveHandles?: () => LiveHandlesSnapshot };

export interface LiveHandlesProbeOptions {
	target?: object;
	win?: EventTarget | null;
	doc?: EventTarget | null;
	captureSite?: () => string;
}

type Fn = (...args: unknown[]) => unknown;
type Kind = 'interval' | 'long_timeout' | 'raf' | 'listener';

interface TimerTarget {
	setInterval?: Fn;
	clearInterval?: Fn;
	setTimeout?: Fn;
	clearTimeout?: Fn;
	requestAnimationFrame?: Fn;
	cancelAnimationFrame?: Fn;
	window?: EventTarget;
	document?: EventTarget;
}

const installed = new WeakMap<object, LiveHandlesProbe>();

function trimPath(raw: string): string {
	let path = raw.replace(/^[a-z][a-z0-9+.-]*:\/\/[^/]*/i, '');
	path = path.replace(/[?#].*$/, '');
	const fs = path.indexOf('/apps/webui/frontend/');
	if (fs >= 0) path = path.slice(fs + '/apps/webui/frontend/'.length);
	const src = path.indexOf('/src/');
	if (src >= 0) path = path.slice(src + 1);
	return path.replace(/^\/+/, '');
}

function frameLocation(line: string): { file: string; line: string } | null {
	const text = line.trim();
	if (!text) return null;
	let loc: string | null = null;
	const v8 = /^at (?:.*?\()?(.+?)\)?$/.exec(text);
	if (v8) loc = v8[1];
	else if (text.includes('@')) loc = text.slice(text.lastIndexOf('@') + 1);
	if (!loc) return null;
	const m = /^(.*?)(?:\?[^:]*)?:(\d+)(?::\d+)?$/.exec(loc);
	if (!m) return null;
	return { file: m[1], line: m[2] };
}

/** Script URL of this module, read from its own stack so hashed prod chunks still match. */
function ownScriptFile(): string | null {
	for (const line of (new Error().stack ?? '').split('\n')) {
		const loc = frameLocation(line);
		if (loc) return loc.file;
	}
	return null;
}

const PROBE_FILE = ownScriptFile();

/**
 * Creator site of the first non-probe frame, as repo-relative `path:line`.
 * Probe frames are skipped by this module's own script URL (works for hashed
 * production chunks) and by name (dev server); `probeFile` overrides the URL.
 */
export function siteFromStack(
	stack: string | undefined,
	probeFile: string | null = PROBE_FILE
): string {
	if (!stack) return 'unknown';
	for (const line of stack.split('\n')) {
		if (line.includes('live-handles-probe')) continue;
		const loc = frameLocation(line);
		if (!loc) continue;
		if (probeFile && loc.file === probeFile) continue;
		const path = trimPath(loc.file);
		if (!path) continue;
		return `${path}:${loc.line}`;
	}
	return 'unknown';
}

function captureFlag(opts: unknown): boolean {
	if (typeof opts === 'boolean') return opts;
	return !!(opts && typeof opts === 'object' && (opts as { capture?: unknown }).capture);
}

export function installLiveHandlesProbe(opts: LiveHandlesProbeOptions = {}): LiveHandlesProbe {
	const target = (opts.target ?? globalThis) as TimerTarget;
	const existing = installed.get(target);
	if (existing) return existing;
	const win = opts.win !== undefined ? opts.win : (target.window ?? null);
	const doc = opts.doc !== undefined ? opts.doc : (target.document ?? null);
	const captureSite = opts.captureSite ?? (() => siteFromStack(new Error().stack));

	const timers = new Map<unknown, { kind: Kind; site: string }>();
	interface ListenerEntry {
		site: string;
		companion: Fn | null;
		abort: { signal: AbortSignal; fn: () => void } | null;
	}
	// target -> type -> listener -> capture -> entry
	const listeners = new Map<EventTarget, Map<string, Map<unknown, Map<boolean, ListenerEntry>>>>();
	const restores: Array<() => void> = [];

	const o = {
		setInterval: target.setInterval,
		clearInterval: target.clearInterval,
		setTimeout: target.setTimeout,
		clearTimeout: target.clearTimeout,
		requestAnimationFrame: target.requestAnimationFrame,
		cancelAnimationFrame: target.cancelAnimationFrame
	};
	const patch = (name: keyof typeof o, fn: Fn): void => {
		const original = o[name];
		if (typeof original !== 'function') return;
		target[name] = fn;
		restores.push(() => {
			target[name] = original;
		});
	};

	patch('setInterval', function (this: unknown, ...args: unknown[]) {
		const handle = (o.setInterval as Fn).apply(this ?? target, args);
		timers.set(handle, { kind: 'interval', site: captureSite() });
		return handle;
	});
	patch('setTimeout', function (this: unknown, fn: unknown, ms: unknown, ...rest: unknown[]) {
		if (typeof fn !== 'function' || !(Number(ms) > LONG_TIMEOUT_MS)) {
			return (o.setTimeout as Fn).call(this ?? target, fn, ms, ...rest);
		}
		let handle: unknown;
		const wrapped = function (this: unknown, ...a: unknown[]) {
			timers.delete(handle);
			return (fn as Fn).apply(this, a);
		};
		handle = (o.setTimeout as Fn).call(this ?? target, wrapped, ms, ...rest);
		timers.set(handle, { kind: 'long_timeout', site: captureSite() });
		return handle;
	});
	// Either clear function clears either kind of handle in browsers.
	patch('clearInterval', function (this: unknown, handle: unknown) {
		timers.delete(handle);
		return (o.clearInterval as Fn).call(this ?? target, handle);
	});
	patch('clearTimeout', function (this: unknown, handle: unknown) {
		timers.delete(handle);
		return (o.clearTimeout as Fn).call(this ?? target, handle);
	});
	patch('requestAnimationFrame', function (this: unknown, cb: unknown) {
		if (typeof cb !== 'function') return (o.requestAnimationFrame as Fn).call(this ?? target, cb);
		let id: unknown;
		const wrapped = function (this: unknown, ...a: unknown[]) {
			timers.delete(id);
			return (cb as Fn).apply(this, a);
		};
		id = (o.requestAnimationFrame as Fn).call(this ?? target, wrapped);
		timers.set(id, { kind: 'raf', site: `raf:${(cb as Fn).name || 'anonymous'}` });
		return id;
	});
	patch('cancelAnimationFrame', function (this: unknown, id: unknown) {
		timers.delete(id);
		return (o.cancelAnimationFrame as Fn).call(this ?? target, id);
	});

	const lookup = (et: EventTarget, type: string, l: unknown, create: boolean) => {
		let byType = listeners.get(et);
		if (!byType) {
			if (!create) return null;
			byType = new Map();
			listeners.set(et, byType);
		}
		let byListener = byType.get(type);
		if (!byListener) {
			if (!create) return null;
			byListener = new Map();
			byType.set(type, byListener);
		}
		let byCapture = byListener.get(l);
		if (!byCapture) {
			if (!create) return null;
			byCapture = new Map();
			byListener.set(l, byCapture);
		}
		return byCapture;
	};

	const patchTarget = (et: EventTarget | null): void => {
		if (!et || typeof et.addEventListener !== 'function') return;
		const host = et as unknown as { addEventListener: Fn; removeEventListener: Fn };
		const add = host.addEventListener;
		const remove = host.removeEventListener;
		const hadOwnAdd = Object.prototype.hasOwnProperty.call(et, 'addEventListener');
		const hadOwnRemove = Object.prototype.hasOwnProperty.call(et, 'removeEventListener');

		const drop = (type: string, l: unknown, capture: boolean): void => {
			const byCapture = lookup(et, type, l, false);
			const entry = byCapture?.get(capture);
			if (!byCapture || !entry) return;
			byCapture.delete(capture);
			if (entry.companion) remove.call(et, type, entry.companion, capture);
			if (entry.abort) entry.abort.signal.removeEventListener('abort', entry.abort.fn);
		};

		host.addEventListener = function (this: unknown, type: unknown, l: unknown, options: unknown) {
			const result = add.call(this ?? et, type, l, options);
			if (l == null) return result;
			const capture = captureFlag(options);
			const key = String(type);
			const byCapture = lookup(et, key, l, true);
			if (!byCapture || byCapture.has(capture)) return result;
			const signal =
				options && typeof options === 'object'
					? (options as { signal?: AbortSignal }).signal
					: undefined;
			if (signal?.aborted) return result;
			const entry: ListenerEntry = { site: captureSite(), companion: null, abort: null };
			if (options && typeof options === 'object' && (options as { once?: unknown }).once) {
				const companion = () => drop(key, l, capture);
				entry.companion = companion;
				add.call(et, key, companion, { once: true, capture });
			}
			if (signal) {
				const fn = () => drop(key, l, capture);
				entry.abort = { signal, fn };
				signal.addEventListener('abort', fn, { once: true });
			}
			byCapture.set(capture, entry);
			return result;
		};
		host.removeEventListener = function (
			this: unknown,
			type: unknown,
			l: unknown,
			options: unknown
		) {
			const result = remove.call(this ?? et, type, l, options);
			drop(String(type), l, captureFlag(options));
			return result;
		};
		restores.push(() => {
			if (hadOwnAdd) host.addEventListener = add;
			else delete (host as Partial<typeof host>).addEventListener;
			if (hadOwnRemove) host.removeEventListener = remove;
			else delete (host as Partial<typeof host>).removeEventListener;
		});
	};
	patchTarget(win);
	if (doc !== win) patchTarget(doc);

	const collect = (): LiveHandlesSnapshot => {
		const snap: LiveHandlesSnapshot = {
			intervals: 0,
			long_timeouts: 0,
			window_listeners: 0,
			rafs: 0,
			by_site: {}
		};
		const charge = (site: string) => {
			snap.by_site[site] = (snap.by_site[site] ?? 0) + 1;
		};
		for (const { kind, site } of timers.values()) {
			if (kind === 'interval') snap.intervals += 1;
			else if (kind === 'long_timeout') snap.long_timeouts += 1;
			else if (kind === 'raf') snap.rafs += 1;
			charge(site);
		}
		for (const byType of listeners.values()) {
			for (const byListener of byType.values()) {
				for (const byCapture of byListener.values()) {
					for (const entry of byCapture.values()) {
						snap.window_listeners += 1;
						charge(entry.site);
					}
				}
			}
		}
		return snap;
	};

	const globalHost = win as unknown as LiveHandlesWindow | null;
	const probe: LiveHandlesProbe = {
		snapshot: collect,
		totals() {
			const { intervals, long_timeouts, window_listeners, rafs } = collect();
			return { intervals, long_timeouts, window_listeners, rafs };
		},
		uninstall() {
			for (const restore of restores.splice(0).reverse()) restore();
			if (globalHost) delete globalHost.__mdtLiveHandles;
			timers.clear();
			listeners.clear();
			installed.delete(target);
		}
	};
	if (globalHost) globalHost.__mdtLiveHandles = () => probe.snapshot();
	installed.set(target, probe);
	return probe;
}
