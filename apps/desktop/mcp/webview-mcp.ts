/**
 * webview-mcp: low-token MCP server for driving and observing the REAL Tauri
 * WKWebView shell during development.
 *
 * Transport seam: the debug binary already hosts a W3C WebDriver HTTP server
 * (tauri-plugin-wdio-webdriver, cfg(debug_assertions) only -- zero release
 * surface). This server is a thin token-shaping layer over that seam plus the
 * engine's own HTTP API. It compiles NO new Rust and adds NO new attack
 * surface: if the shell is a release build, every tool fails loudly.
 *
 * Layering (cheapest first -- callers should escalate, not default, upward):
 *   app_state   structured JSON straight from the engine      (~0.2-0.6k tok)
 *   ui_tree     a11y-style tree of the live DOM with refs     (~0.5-2k tok)
 *   screenshot  native WKWebView snapshot, cropped + descaled (~1.6k tok)
 *   act         click/type/chord by ref, auto-returns a DIFF of the tree
 *   eval_js     escape hatch, raw JS in the page
 *
 * Known input limits of the embedded driver (measured in the tier-2 suite,
 * apps/desktop/tests/real-shell-smoke.e2e.ts): plain element clicks reach the
 * page; hover and double-click do not; keyboard chords may not deliver. act()
 * therefore dispatches synthetic DOM events and SAYS SO in every result --
 * delivery is labeled, never silently substituted.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ connect to a running debug shell + engine via env, fail fast if absent
 *   ✔︎ ✅ ui_tree: visible interactive elements w/ refs, version-stamped
 *   ✔︎ ✅ act: stale-ref rejection + post-action tree diff in the same result
 *   ✔︎ ✅ screenshot: descaled to CSS pixels (never ship retina 2x), croppable
 *   ✔︎ ✅ app_state: GET-only proxy onto the engine's /api/v1 surface
 *   → agent loop / compound DJ tools (load_track_to_deck etc): later, on top
 *
 * Acceptance tests (asserted by mcp/smoke.ts):
 *   [if] MDT_WEBVIEW_MCP_WEBDRIVER unset [then ⛔️] server exits nonzero at boot
 *   [if] act() is given a ref from an older ui_tree version [then ⛔️] error
 *        tells the caller to re-observe; nothing is clicked
 *   [if] screenshot of a 2x display [then] output width equals CSS width
 */
import {
	appendFileSync,
	existsSync,
	mkdirSync,
	readFileSync,
	readdirSync,
	rmSync,
	writeFileSync
} from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';
import { z } from 'zod';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';

//----- config: explicit, no hidden defaults ---------------------------------

function _requireEnv(name: string): string {
	const value = process.env[name];
	if (!value) {
		throw new Error(
			`${name} is required (e.g. MDT_WEBVIEW_MCP_WEBDRIVER=http://127.0.0.1:4455 ` +
				`MDT_WEBVIEW_MCP_ENGINE=http://127.0.0.1:8691). Start the debug shell with ` +
				`TAURI_WEBDRIVER_PORT set; release builds have no WebDriver surface.`
		);
	}
	return value;
}

const WEBDRIVER_BASE = _requireEnv('MDT_WEBVIEW_MCP_WEBDRIVER');
const ENGINE_BASE = _requireEnv('MDT_WEBVIEW_MCP_ENGINE');
const MAX_IMAGE_WIDTH_DEFAULT = 1456; // ~1585 tokens at Anthropic's (w*h)/750
const DIFF_LINE_CAP = 80;
const ACT_SETTLE_MS = 300;

/**
 * Session traces: Devin-style visual replay of everything this MCP did.
 * Default root is documented (README + status tool), overridable, and 'off'
 * disables recording entirely. Traces expire: anything older than
 * TRACE_TTL_HOURS is deleted on server boot (creation time from meta.json,
 * never guessed from dir names; unknown dirs are left alone).
 */
const TRACE_ROOT =
	process.env.MDT_WEBVIEW_MCP_TRACE_DIR ?? join(homedir(), '.cache', 'opendj-webview-mcp', 'traces');
const TRACE_ENABLED = TRACE_ROOT !== 'off';
const TRACE_TTL_HOURS = 48;

//----- trace recording --------------------------------------------------------

interface TraceEvent {
	t: number; // ms since session start
	tool: string;
	detail: string;
	caption?: string; // drawn on the frame in the player (typed text, chords)
	rect?: { x: number; y: number; w: number; h: number }; // CSS px click target
	viewport?: { w: number; h: number };
	frame?: string; // relative path of the frame showing state AFTER this event
}

class Trace {
	readonly dir: string;
	private readonly _startedAt = Date.now();
	private readonly _events: TraceEvent[] = [];
	private _frameCounter = 0;
	private readonly _template: string;

	constructor() {
		const stamp = new Date(this._startedAt).toISOString().replace(/[:.]/g, '-');
		this.dir = join(TRACE_ROOT, `${stamp}-pid${process.pid}`);
		mkdirSync(join(this.dir, 'frames'), { recursive: true });
		writeFileSync(
			join(this.dir, 'meta.json'),
			JSON.stringify({ session: this.dir, started_epoch_ms: this._startedAt, pid: process.pid })
		);
		appendFileSync(
			join(TRACE_ROOT, 'index.jsonl'),
			`${JSON.stringify({ kind: 'start', dir: this.dir, started: new Date(this._startedAt).toISOString() })}\n`
		);
		this._template = readFileSync(
			join(dirname(fileURLToPath(import.meta.url)), 'trace-player.html'),
			'utf8'
		);
	}

	record(event: Omit<TraceEvent, 't'>): void {
		this._events.push({ t: Date.now() - this._startedAt, ...event });
		appendFileSync(join(this.dir, 'events.jsonl'), `${JSON.stringify(this._events.at(-1))}\n`);
		// Rewritten per event on purpose: the player stays valid even if this
		// process is killed mid-session (events are tiny; frames are the weight).
		writeFileSync(
			join(this.dir, 'trace.html'),
			this._template.replace('/*__EVENTS__*/[]', JSON.stringify(this._events))
		);
	}

	saveFrame(png: Buffer): string {
		this._frameCounter += 1;
		const name = `frames/${String(this._frameCounter).padStart(3, '0')}.png`;
		writeFileSync(join(this.dir, name), png);
		return name;
	}

	static pruneExpired(): void {
		if (!existsSync(TRACE_ROOT)) return;
		const cutoff = Date.now() - TRACE_TTL_HOURS * 3_600_000;
		const kept: string[] = [];
		for (const entry of readdirSync(TRACE_ROOT, { withFileTypes: true })) {
			if (!entry.isDirectory()) continue;
			const sessionDir = join(TRACE_ROOT, entry.name);
			const metaPath = join(sessionDir, 'meta.json');
			if (!existsSync(metaPath)) continue; // not ours to delete
			const meta = JSON.parse(readFileSync(metaPath, 'utf8')) as { started_epoch_ms: number };
			if (meta.started_epoch_ms < cutoff) {
				rmSync(sessionDir, { recursive: true, force: true });
			} else {
				kept.push(sessionDir);
			}
		}
		const indexPath = join(TRACE_ROOT, 'index.jsonl');
		if (existsSync(indexPath)) {
			const survivors = readFileSync(indexPath, 'utf8')
				.split('\n')
				.filter((line) => line && kept.some((dir) => line.includes(dir)));
			writeFileSync(indexPath, survivors.length ? `${survivors.join('\n')}\n` : '');
		}
	}
}

let trace: Trace | null = null;
if (TRACE_ENABLED) {
	mkdirSync(TRACE_ROOT, { recursive: true });
	Trace.pruneExpired();
	trace = new Trace();
}

/** Full-window frame for the trace, same descale rules as the screenshot tool. */
async function _traceFrame(): Promise<string | undefined> {
	if (!trace) return undefined;
	const b64 = await wd.screenshotBase64();
	const page = await wd.execute<{ w: number }>('return { w: window.innerWidth };');
	const png = await sharp(Buffer.from(b64, 'base64'))
		.resize({ width: Math.min(MAX_IMAGE_WIDTH_DEFAULT, page.w), withoutEnlargement: true })
		.png()
		.toBuffer();
	return trace.saveFrame(png);
}

//----- minimal raw W3C WebDriver client -------------------------------------

class Wd {
	private _sessionId: string | null = null;

	async ensureSession(): Promise<string> {
		if (this._sessionId) return this._sessionId;
		const res = await this._post('/session', {
			capabilities: { alwaysMatch: { browserName: 'tauri' } }
		});
		this._sessionId = (res as { sessionId?: string; value?: { sessionId?: string } }).sessionId
			?? (res as { value: { sessionId: string } }).value.sessionId;
		if (!this._sessionId) throw new Error(`WebDriver session create returned no sessionId`);
		return this._sessionId;
	}

	async execute<T>(script: string, args: unknown[] = []): Promise<T> {
		const sid = await this.ensureSession();
		const res = await this._post(`/session/${sid}/execute/sync`, {
			script,
			args
		});
		return (res as { value: T }).value;
	}

	async navigate(url: string): Promise<void> {
		const sid = await this.ensureSession();
		await this._post(`/session/${sid}/url`, { url });
	}

	async screenshotBase64(): Promise<string> {
		const sid = await this.ensureSession();
		const res = await this._get(`/session/${sid}/screenshot`);
		return (res as { value: string }).value;
	}

	async status(): Promise<unknown> {
		return this._get('/status');
	}

	private async _post(path: string, body: unknown): Promise<unknown> {
		return this._req('POST', path, body);
	}
	private async _get(path: string): Promise<unknown> {
		return this._req('GET', path);
	}
	private async _req(method: string, path: string, body?: unknown): Promise<unknown> {
		let response: Response;
		try {
			response = await fetch(`${WEBDRIVER_BASE}${path}`, {
				method,
				headers: { 'content-type': 'application/json' },
				body: body === undefined ? undefined : JSON.stringify(body)
			});
		} catch (error) {
			throw new Error(
				`no WebDriver at ${WEBDRIVER_BASE} (${String(error)}). Is the DEBUG shell ` +
					`running with TAURI_WEBDRIVER_PORT? Release builds expose no WebDriver.`
			);
		}
		const json = (await response.json()) as { value?: { error?: string; message?: string } };
		if (!response.ok) {
			// invalid session: drop it so the next call re-creates
			if (json.value?.error === 'invalid session id') this._sessionId = null;
			throw new Error(
				`WebDriver ${method} ${path} -> ${response.status}: ` +
					`${json.value?.error ?? '?'}: ${json.value?.message ?? JSON.stringify(json)}`
			);
		}
		return json;
	}
}

const wd = new Wd();

//----- injected page scripts -------------------------------------------------

/**
 * Serializes visible, meaningful DOM into an indented a11y-style tree and
 * stashes ref -> Element on the page under a monotonically increasing version.
 * Runs inside the page via execute/sync; the same JS world persists between
 * calls, which is what makes refs resolvable later.
 */
const SERIALIZE_SCRIPT = `
const rootSelector = arguments[0];
const w = window;
const root = rootSelector ? document.querySelector(rootSelector) : document.body;
if (!root) return { error: 'no element matches selector: ' + rootSelector };
const version = (w.__mdtTreeVersion = (w.__mdtTreeVersion || 0) + 1);
const refMap = {};
w.__mdtRefs = { version: version, map: refMap };
const SKIP = { SCRIPT: 1, STYLE: 1, LINK: 1, META: 1, TEMPLATE: 1, NOSCRIPT: 1, SVG: 1, PATH: 1 };
const IMPLICIT_ROLE = {
  A: 'link', BUTTON: 'button', SELECT: 'combobox', TEXTAREA: 'textbox',
  H1: 'heading', H2: 'heading', H3: 'heading', H4: 'heading',
  NAV: 'nav', HEADER: 'header', FOOTER: 'footer', MAIN: 'main', DIALOG: 'dialog',
  TABLE: 'table', INPUT: 'textbox', LABEL: 'label', OPTION: 'option', LI: 'listitem'
};
// hardHidden prunes the SUBTREE; zeroRect only suppresses the node's own line.
// A zero-height wrapper with visible overflowing children is common (SvelteKit
// mount divs) and pruning there once serialized an entire live app to nothing.
function hardHidden(el) {
  if (el.hidden || el.getAttribute('aria-hidden') === 'true') return true;
  const s = getComputedStyle(el);
  return s.display === 'none' || s.visibility === 'hidden';
}
function zeroRect(el) {
  const r = el.getBoundingClientRect();
  return r.width === 0 && r.height === 0;
}
function roleOf(el) {
  const explicit = el.getAttribute('role');
  if (explicit) return explicit;
  if (el.tagName === 'INPUT') {
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    if (t === 'checkbox') return 'checkbox';
    if (t === 'radio') return 'radio';
    if (t === 'range') return 'slider';
    if (t === 'button' || t === 'submit') return 'button';
    return 'textbox';
  }
  return IMPLICIT_ROLE[el.tagName] || null;
}
function nameOf(el) {
  const aria = el.getAttribute('aria-label');
  if (aria) return aria;
  const title = el.getAttribute('title');
  if (title) return title;
  if (el.labels && el.labels[0]) return el.labels[0].textContent.trim();
  const own = Array.from(el.childNodes)
    .filter(function (n) { return n.nodeType === 3; })
    .map(function (n) { return n.textContent.trim(); })
    .join(' ').trim();
  if (own) return own.slice(0, 80);
  if (el.children.length === 0 && el.textContent) return el.textContent.trim().slice(0, 80);
  return null;
}
function stateOf(el) {
  const parts = [];
  if (el.disabled) parts.push('disabled');
  if (el.checked) parts.push('checked');
  if (el.getAttribute('aria-pressed') !== null) parts.push('pressed=' + el.getAttribute('aria-pressed'));
  if (el.getAttribute('aria-valuenow') !== null) parts.push('value=' + el.getAttribute('aria-valuenow'));
  if (el.getAttribute('aria-expanded')) parts.push('expanded=' + el.getAttribute('aria-expanded'));
  if (el.getAttribute('aria-selected') === 'true') parts.push('selected');
  if (el.value !== undefined && el.value !== '' && el.tagName !== 'BUTTON' && typeof el.value === 'string') {
    parts.push('value=' + String(el.value).slice(0, 40));
  }
  return parts;
}
const lines = [];
let refCounter = 0;
function walk(el, depth) {
  if (SKIP[el.tagName] || hardHidden(el)) return;
  const role = roleOf(el);
  const name = nameOf(el);
  const testid = el.getAttribute('data-testid');
  const interactive = role && role !== 'label' && role !== 'listitem' && role !== 'heading'
    && role !== 'nav' && role !== 'header' && role !== 'footer' && role !== 'main' && role !== 'table';
  const meaningful = !zeroRect(el) && (role || testid || (name && el.children.length === 0));
  if (meaningful) {
    let line = '  '.repeat(depth) + (role || el.tagName.toLowerCase());
    if (name) line += ' "' + name.replace(/"/g, "'") + '"';
    if (testid) line += ' #' + testid;
    const state = stateOf(el);
    if (state.length) line += ' [' + state.join(' ') + ']';
    if (interactive || testid) {
      refCounter += 1;
      const ref = 'e' + refCounter;
      refMap[ref] = el;
      line += ' @' + ref;
    }
    lines.push(line);
    depth += 1;
  }
  for (const child of el.children) walk(child, depth);
}
walk(root, 0);
return {
  version: version,
  url: location.pathname,
  title: document.title,
  tree: lines.join('\\n'),
  refCount: refCounter,
  devicePixelRatio: w.devicePixelRatio,
  viewport: { w: w.innerWidth, h: w.innerHeight }
};
`;

/**
 * Resolves a versioned ref and performs one action with synthetic DOM events.
 * Synthetic is deliberate and labeled: the embedded driver's native input path
 * drops hover/double-click/some chords (measured in tier 2), and the app's own
 * listeners (e.g. hotkeys.ts) are window-level capture-phase, which synthetic
 * events reach identically.
 */
const ACT_SCRIPT = `
const kind = arguments[0], ref = arguments[1], expectVersion = arguments[2], text = arguments[3];
const store = window.__mdtRefs;
if (ref) {
  if (!store) return { error: 'no ui_tree snapshot exists yet: call ui_tree first' };
  if (store.version !== expectVersion) {
    return { error: 'stale ref: tree is at version ' + store.version + ', ref came from ' +
      expectVersion + '. Re-observe with ui_tree.' };
  }
  if (!store.map[ref]) return { error: 'unknown ref ' + ref + ' in version ' + expectVersion };
}
const el = ref ? store.map[ref] : null;
if (kind === 'click') {
  if (!el) return { error: 'click needs a ref' };
  const r = el.getBoundingClientRect();
  const opts = { bubbles: true, cancelable: true, view: window,
    clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 };
  el.dispatchEvent(new PointerEvent('pointerdown', opts));
  el.dispatchEvent(new MouseEvent('mousedown', opts));
  el.dispatchEvent(new PointerEvent('pointerup', opts));
  el.dispatchEvent(new MouseEvent('mouseup', opts));
  el.click();
  return { ok: true, delivery: 'synthetic-dom-events',
    rect: { x: r.x, y: r.y, w: r.width, h: r.height },
    viewport: { w: window.innerWidth, h: window.innerHeight } };
}
if (kind === 'type') {
  if (!el) return { error: 'type needs a ref' };
  el.focus();
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value');
  if (!setter || !setter.set) return { error: 'element has no value setter: ' + el.tagName };
  setter.set.call(el, text);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  const tr = el.getBoundingClientRect();
  return { ok: true, delivery: 'synthetic-dom-events', value: el.value,
    rect: { x: tr.x, y: tr.y, w: tr.width, h: tr.height },
    viewport: { w: window.innerWidth, h: window.innerHeight } };
}
if (kind === 'chord') {
  const parts = text.split('+');
  const key = parts[parts.length - 1];
  const init = { key: key, bubbles: true, cancelable: true,
    metaKey: parts.includes('Meta'), ctrlKey: parts.includes('Ctrl'),
    altKey: parts.includes('Alt'), shiftKey: parts.includes('Shift') };
  const target = el || window;
  target.dispatchEvent(new KeyboardEvent('keydown', init));
  target.dispatchEvent(new KeyboardEvent('keyup', init));
  return { ok: true, delivery: 'synthetic-KeyboardEvent', chord: text };
}
return { error: 'unknown act kind: ' + kind };
`;

const RECT_SCRIPT = `
const selector = arguments[0];
const el = document.querySelector(selector);
if (!el) return { error: 'no element matches selector: ' + selector };
const r = el.getBoundingClientRect();
return { x: r.x, y: r.y, w: r.width, h: r.height, dpr: window.devicePixelRatio };
`;

//----- tree cache + diffing ---------------------------------------------------

interface TreeSnapshot {
	version: number;
	lines: string[];
}
let lastTree: TreeSnapshot | null = null;

function _diffTrees(before: string[], after: string[]): string {
	const beforeSet = new Set(before);
	const afterSet = new Set(after);
	const removed = before.filter((l) => !afterSet.has(l));
	const added = after.filter((l) => !beforeSet.has(l));
	if (removed.length === 0 && added.length === 0) return '(no tree change)';
	if (removed.length + added.length > DIFF_LINE_CAP) {
		// Preview the SHALLOWEST new nodes: they name what appeared (a dialog, a
		// panel) without paying for its full contents.
		const indentOf = (l: string): number => l.length - l.trimStart().length;
		const preview = [...added]
			.sort((a, b) => indentOf(a) - indentOf(b))
			.slice(0, 12)
			.map((l) => `+ ${l.trim()}`);
		return (
			`(large change: ${removed.length} lines gone, ${added.length} new -- ` +
			`re-observe with ui_tree. Shallowest new nodes:)\n${preview.join('\n')}`
		);
	}
	return [...removed.map((l) => `- ${l.trim()}`), ...added.map((l) => `+ ${l.trim()}`)].join('\n');
}

interface SerializedTree {
	error?: string;
	version: number;
	url: string;
	title: string;
	tree: string;
	refCount: number;
	devicePixelRatio: number;
	viewport: { w: number; h: number };
}

async function _snapshotTree(selector?: string): Promise<SerializedTree> {
	const result = await wd.execute<SerializedTree>(SERIALIZE_SCRIPT, [selector ?? null]);
	if (result.error) throw new Error(result.error);
	lastTree = { version: result.version, lines: result.tree.split('\n') };
	return result;
}

//----- MCP server -------------------------------------------------------------

const server = new McpServer({ name: 'opendj-webview-mcp', version: '0.1.0' });

server.registerTool(
	'status',
	{
		description:
			'Health of the whole seam: embedded WebDriver reachable, engine healthy, current page. Call first.'
	},
	async () => {
		const driver = await wd.status();
		const engineRes = await fetch(`${ENGINE_BASE}/api/v1/health`).catch((e: unknown) => e);
		const engine =
			engineRes instanceof Response && engineRes.ok
				? await engineRes.json()
				: `UNREACHABLE: ${String(engineRes instanceof Response ? engineRes.status : engineRes)}`;
		const page = await wd.execute<{ title: string; url: string }>(
			'return { title: document.title, url: location.href };'
		);
		return {
			content: [
				{
					type: 'text',
					text: JSON.stringify(
						{ webdriver: driver, engine, page, trace: trace ? trace.dir : 'off' },
						null,
						1
					)
				}
			]
		};
	}
);

server.registerTool(
	'app_state',
	{
		description:
			'Structured JSON straight from the engine HTTP API (cheapest observation). ' +
			'GET-only, /api/v1 paths only. Default returns setup/status. For mutations use the engine API directly.',
		inputSchema: { path: z.string().optional().describe('e.g. /api/v1/setup/status') }
	},
	async ({ path }) => {
		const target = path ?? '/api/v1/setup/status';
		if (!target.startsWith('/api/v1/')) {
			throw new Error(`app_state only proxies /api/v1/* paths, got: ${target}`);
		}
		const response = await fetch(`${ENGINE_BASE}${target}`);
		const body = await response.text();
		if (!response.ok) throw new Error(`engine GET ${target} -> ${response.status}: ${body}`);
		trace?.record({ tool: 'app_state', detail: target });
		return { content: [{ type: 'text', text: body }] };
	}
);

server.registerTool(
	'navigate',
	{
		description:
			'Point the real shell webview at an app route (e.g. "/performance", "/setup"). ' +
			'Accepts a path (resolved against the engine origin) or a full URL. Invalidates all refs.',
		inputSchema: { url: z.string() }
	},
	async ({ url }) => {
		const absolute = url.startsWith('http') ? url : `${ENGINE_BASE}${url}`;
		await wd.navigate(absolute);
		lastTree = null;
		await new Promise((resolve) => setTimeout(resolve, ACT_SETTLE_MS));
		trace?.record({ tool: 'navigate', detail: absolute, frame: await _traceFrame() });
		return { content: [{ type: 'text', text: `navigated to ${absolute}. Re-observe with ui_tree.` }] };
	}
);

server.registerTool(
	'ui_tree',
	{
		description:
			'A11y-style tree of the live shell DOM. Interactive nodes carry @refs for act(). ' +
			'Refs are version-stamped; any act() after the page changes must re-observe.',
		inputSchema: {
			selector: z.string().optional().describe('CSS selector to scope the tree (default: body)')
		}
	},
	async ({ selector }) => {
		const snap = await _snapshotTree(selector);
		const header = `[v${snap.version}] ${snap.title} ${snap.url} viewport=${snap.viewport.w}x${snap.viewport.h} refs=${snap.refCount}`;
		trace?.record({ tool: 'ui_tree', detail: `v${snap.version} ${snap.url} refs=${snap.refCount}` });
		return { content: [{ type: 'text', text: `${header}\n${snap.tree}` }] };
	}
);

server.registerTool(
	'screenshot',
	{
		description:
			'Native WKWebView snapshot (no screen-recording permission involved). Descaled from retina ' +
			'to CSS pixels so text stays 1:1 crisp for vision models. Prefer selector-cropped shots.',
		inputSchema: {
			selector: z.string().optional().describe('crop to this element'),
			max_width: z.number().optional().describe(`cap output width (default ${MAX_IMAGE_WIDTH_DEFAULT})`)
		}
	},
	async ({ selector, max_width }) => {
		const b64 = await wd.screenshotBase64();
		let image = sharp(Buffer.from(b64, 'base64'));
		const meta = await image.metadata();
		const page = await wd.execute<{ dpr: number; w: number }>(
			'return { dpr: window.devicePixelRatio, w: window.innerWidth };'
		);
		const scale = meta.width && page.w ? meta.width / page.w : page.dpr;
		if (selector) {
			const rect = await wd.execute<{ error?: string; x: number; y: number; w: number; h: number }>(
				RECT_SCRIPT,
				[selector]
			);
			if (rect.error) throw new Error(rect.error);
			image = image.extract({
				left: Math.max(0, Math.round(rect.x * scale)),
				top: Math.max(0, Math.round(rect.y * scale)),
				width: Math.round(rect.w * scale),
				height: Math.round(rect.h * scale)
			});
		}
		const cssWidth = selector
			? undefined // extract() already changed dims; resize below normalizes
			: page.w;
		const targetWidth = Math.min(
			max_width ?? MAX_IMAGE_WIDTH_DEFAULT,
			cssWidth ?? Math.round((meta.width ?? 0) / scale)
		);
		const png = await image.resize({ width: targetWidth, withoutEnlargement: true }).png().toBuffer();
		trace?.record({
			tool: 'screenshot',
			detail: selector ?? 'full window',
			frame: trace.saveFrame(png)
		});
		return {
			content: [{ type: 'image', data: png.toString('base64'), mimeType: 'image/png' }]
		};
	}
);

server.registerTool(
	'act',
	{
		description:
			'One interaction in the real shell: click a @ref, type into a @ref, or send a key chord ' +
			'(e.g. "Meta+," / "Escape"). Delivery is synthetic DOM events (labeled in the result; the ' +
			"embedded driver's native path drops hover/dblclick/some chords). Auto-returns the tree DIFF.",
		inputSchema: {
			kind: z.enum(['click', 'type', 'chord']),
			ref: z.string().optional().describe('a @ref from the LATEST ui_tree'),
			tree_version: z.number().optional().describe('the [vN] the ref came from'),
			text: z.string().optional().describe('text for type, or chord like "Meta+," / "Escape"')
		}
	},
	async ({ kind, ref, tree_version, text }) => {
		if (ref && tree_version === undefined) {
			throw new Error('act with a ref requires tree_version (the [vN] header from ui_tree)');
		}
		if ((kind === 'type' || kind === 'chord') && !text) {
			throw new Error(`act kind=${kind} requires text`);
		}
		const before = lastTree;
		const result = await wd.execute<{
			error?: string;
			ok?: boolean;
			delivery?: string;
			rect?: { x: number; y: number; w: number; h: number };
			viewport?: { w: number; h: number };
		}>(ACT_SCRIPT, [kind, ref ?? null, tree_version ?? null, text ?? null]);
		if (result.error) throw new Error(result.error);
		await new Promise((resolve) => setTimeout(resolve, ACT_SETTLE_MS));
		trace?.record({
			tool: 'act',
			detail: `${kind} ${ref ?? ''} ${text ?? ''}`.trim(),
			caption: kind === 'chord' ? `chord: ${text}` : kind === 'type' ? `typed: "${text}"` : undefined,
			rect: result.rect,
			viewport: result.viewport,
			frame: await _traceFrame()
		});
		const after = await _snapshotTree();
		const diff = before
			? _diffTrees(before.lines, after.tree.split('\n'))
			: '(no prior tree to diff against)';
		return {
			content: [
				{
					type: 'text',
					text:
						`${kind} ok via ${result.delivery}. Tree now [v${after.version}] ` +
						`(refs from earlier versions are stale).\nDIFF:\n${diff}`
				}
			]
		};
	}
);

server.registerTool(
	'eval_js',
	{
		description:
			'Escape hatch: run JS in the live page, JSON result (capped 8000 chars). ' +
			'Use a `return` statement, e.g. "return document.title".',
		inputSchema: { script: z.string() }
	},
	async ({ script }) => {
		const value = await wd.execute<unknown>(script);
		trace?.record({ tool: 'eval_js', detail: script.slice(0, 80) });
		const text = JSON.stringify(value);
		return {
			content: [{ type: 'text', text: text.length > 8000 ? `${text.slice(0, 8000)}...(capped)` : text }]
		};
	}
);

await server.connect(new StdioServerTransport());
