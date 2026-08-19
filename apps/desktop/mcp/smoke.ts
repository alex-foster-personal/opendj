/**
 * End-to-end smoke for webview-mcp: real fixture engine, real DEBUG shell,
 * real MCP stdio transport. No mocks anywhere. Exits nonzero on ANY failure.
 *
 * Ports: engine 8698, WebDriver 4456 -- deliberately distinct from every
 * reserved suite port (tier-1 8690, tier-2 8691/4455, boot test 8697, lanes).
 *
 * Acceptance tests:
 *   [if] the tree lacks the app's known landmarks [then ⛔️] fail, not warn
 *   [if] act(chord Meta+,) does not surface the Settings dialog [then ⛔️] fail
 *   [if] act() with a stale ref does NOT error [then ⛔️] fail (staleness is
 *        load-bearing: agents must be forced to re-observe)
 *   [if] screenshot width exceeds the CSS viewport width [then ⛔️] fail
 *        (retina 2x leaking through = mushy text for vision models)
 */
import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const DESKTOP_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPOSITORY_ROOT = join(DESKTOP_ROOT, '..', '..');
const APP_BINARY = join(DESKTOP_ROOT, 'src-tauri', 'target', 'debug', 'opendj-desktop');
const FIXTURE_BUILDER = join(
	REPOSITORY_ROOT,
	'apps/webui/frontend/tests/e2e/support/deckload_fixture.py'
);

const ENGINE_PORT = 8698;
const WEBDRIVER_PORT = 4456;
const ENGINE_ORIGIN = `http://127.0.0.1:${ENGINE_PORT}`;
const WEBDRIVER_ORIGIN = `http://127.0.0.1:${WEBDRIVER_PORT}`;
const DATA_DIR = join(DESKTOP_ROOT, 'mcp', '.smoke-data');
const SANDBOX_HOME = join(DATA_DIR, 'sandbox-home');
const TRACE_ROOT = join(DATA_DIR, 'traces');

const children: ChildProcess[] = [];
let failures = 0;

function check(label: string, condition: boolean, detail: string): void {
	if (condition) {
		console.log(`[OK] ${label}`);
	} else {
		failures += 1;
		console.error(`[ERROR] ${label}: ${detail}`);
	}
}

async function waitForHttp(url: string, timeoutMs: number): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	let lastError = 'never attempted';
	while (Date.now() < deadline) {
		try {
			const response = await fetch(url);
			if (response.ok) return;
			lastError = `HTTP ${response.status}`;
		} catch (error) {
			lastError = String(error);
		}
		await new Promise((resolve) => setTimeout(resolve, 500));
	}
	throw new Error(`${url} never became ready: ${lastError}`);
}

function textOf(result: unknown): string {
	const content = (result as { content: Array<{ type: string; text?: string }> }).content;
	return content
		.filter((c) => c.type === 'text')
		.map((c) => c.text)
		.join('\n');
}

async function main(): Promise<void> {
	if (!existsSync(APP_BINARY)) {
		throw new Error(`debug shell missing: ${APP_BINARY} (cd apps/desktop/src-tauri && cargo build)`);
	}

	//----- boot engine over a fresh fixture -----
	rmSync(DATA_DIR, { recursive: true, force: true });
	mkdirSync(SANDBOX_HOME, { recursive: true });
	const built = spawn('uv', ['run', '--no-sync', 'python', FIXTURE_BUILDER, '--data-dir', DATA_DIR], {
		cwd: REPOSITORY_ROOT,
		stdio: 'inherit'
	});
	const buildCode: number = await new Promise((resolve) => built.on('exit', resolve));
	if (buildCode !== 0) throw new Error(`fixture builder exited ${buildCode}`);

	const engine = spawn(
		'uv',
		['run', '--no-sync', 'python', '-m', 'apps.engine_core', 'serve', '--data-dir', DATA_DIR, '--host', '127.0.0.1', '--port', String(ENGINE_PORT)],
		{
			cwd: REPOSITORY_ROOT,
			stdio: 'inherit',
			env: { ...process.env, MDT_DATA_DIR: DATA_DIR, WEB_CONCURRENCY: '', HOME: SANDBOX_HOME }
		}
	);
	children.push(engine);
	await waitForHttp(`${ENGINE_ORIGIN}/api/v1/health`, 120_000);

	//----- boot the debug shell with the embedded WebDriver -----
	const shell = spawn(APP_BINARY, [], {
		env: {
			...process.env,
			OPENDJ_ENGINE_ORIGIN: ENGINE_ORIGIN,
			TAURI_WEBDRIVER_PORT: String(WEBDRIVER_PORT),
			HOME: SANDBOX_HOME
		},
		stdio: 'ignore'
	});
	children.push(shell);
	await waitForHttp(`${WEBDRIVER_ORIGIN}/status`, 60_000);

	//----- pre-seed an EXPIRED trace session: boot must prune it -----
	const expiredDir = join(TRACE_ROOT, 'expired-session');
	mkdirSync(expiredDir, { recursive: true });
	writeFileSync(
		join(expiredDir, 'meta.json'),
		JSON.stringify({ started_epoch_ms: Date.now() - 72 * 3_600_000 })
	);
	writeFileSync(join(TRACE_ROOT, 'index.jsonl'), `{"kind":"start","dir":"${expiredDir}"}\n`);

	//----- connect an MCP client over stdio, exactly as an agent would -----
	const transport = new StdioClientTransport({
		command: 'pnpm',
		args: ['exec', 'tsx', join(DESKTOP_ROOT, 'mcp', 'webview-mcp.ts')],
		cwd: DESKTOP_ROOT,
		env: {
			...process.env,
			MDT_WEBVIEW_MCP_WEBDRIVER: WEBDRIVER_ORIGIN,
			MDT_WEBVIEW_MCP_ENGINE: ENGINE_ORIGIN,
			MDT_WEBVIEW_MCP_TRACE_DIR: TRACE_ROOT
		}
	});
	const client = new Client({ name: 'webview-mcp-smoke', version: '0.0.1' });
	await client.connect(transport);

	// give the shell time to leave the bootstrap page, then pin the route the
	// same way tier 2 does -- hotkeys and decks live under /performance
	await new Promise((resolve) => setTimeout(resolve, 4000));
	const nav = textOf(
		await client.callTool({ name: 'navigate', arguments: { url: '/performance' } })
	);
	check('navigate lands on /performance', nav.includes('/performance'), nav);
	await new Promise((resolve) => setTimeout(resolve, 2000));

	//----- 1: status -----
	const status = textOf(await client.callTool({ name: 'status', arguments: {} }));
	check('status reaches both seams', status.includes('"ready"') || status.includes('ok'), status.slice(0, 300));
	check('status shows engine health', status.includes('engine'), status.slice(0, 300));

	//----- 2: app_state -----
	const state = textOf(await client.callTool({ name: 'app_state', arguments: {} }));
	check('app_state returns setup status JSON', state.includes('{'), state.slice(0, 200));

	//----- 3: ui_tree -----
	const tree = textOf(await client.callTool({ name: 'ui_tree', arguments: {} }));
	check('ui_tree is version 1', tree.startsWith('[v1]'), tree.slice(0, 120));
	check('ui_tree carries refs', /@e\d+/.test(tree), tree.slice(0, 400));
	const treeTokensApprox = Math.round(tree.length / 4);
	console.log(`[INFO] ui_tree size: ${tree.length} chars (~${treeTokensApprox} tokens)`);

	//----- 4: act chord opens Settings, diff shows it -----
	const chord = textOf(
		await client.callTool({ name: 'act', arguments: { kind: 'chord', text: 'Meta+,' } })
	);
	check('Meta+, surfaces the Settings dialog in the DIFF', chord.includes('Settings'), chord.slice(0, 600));
	check('act result labels its delivery', chord.includes('synthetic'), chord.slice(0, 200));

	//----- 5: stale-ref rejection -----
	const stale = (await client.callTool({
		name: 'act',
		arguments: { kind: 'click', ref: 'e1', tree_version: 1 }
	})) as { isError?: boolean };
	const staleText = textOf(stale);
	check(
		'stale ref is rejected with a re-observe instruction',
		staleText.includes('stale ref') && staleText.includes('e-observe'),
		`isError=${String(stale.isError)}: ${staleText.slice(0, 300)}`
	);

	//----- 6: fresh tree, Escape closes settings -----
	const tree2 = textOf(await client.callTool({ name: 'ui_tree', arguments: {} }));
	check('settings dialog present in fresh tree', tree2.includes('Settings'), tree2.slice(0, 400));
	const escape = textOf(
		await client.callTool({ name: 'act', arguments: { kind: 'chord', text: 'Escape' } })
	);
	check('Escape closes the dialog (diff shows removal)', escape.includes('- '), escape.slice(0, 600));

	//----- 6b: a REAL click by fresh ref (also feeds the trace a click rect) -----
	const tree3 = textOf(await client.callTool({ name: 'ui_tree', arguments: {} }));
	const v3 = Number(tree3.match(/^\[v(\d+)\]/)?.[1]);
	const settingsBtnRef = tree3
		.split('\n')
		.find((l) => l.includes('"Open settings"'))
		?.match(/@(e\d+)/)?.[1];
	check('fresh tree has the Open settings button', !!settingsBtnRef && Number.isFinite(v3), tree3.slice(0, 300));
	if (settingsBtnRef) {
		const clicked = textOf(
			await client.callTool({
				name: 'act',
				arguments: { kind: 'click', ref: settingsBtnRef, tree_version: v3 }
			})
		);
		check('clicking Open settings by ref reopens the dialog', clicked.includes('Settings'), clicked.slice(0, 500));
		await client.callTool({ name: 'act', arguments: { kind: 'chord', text: 'Escape' } });
	}

	//----- 7: screenshot descaled to CSS pixels -----
	const shot = (await client.callTool({ name: 'screenshot', arguments: {} })) as {
		content: Array<{ type: string; data?: string; mimeType?: string }>;
	};
	const img = shot.content.find((c) => c.type === 'image');
	check('screenshot returns a PNG', img?.mimeType === 'image/png' && !!img.data, JSON.stringify(shot).slice(0, 200));
	if (img?.data) {
		const buffer = Buffer.from(img.data, 'base64');
		// PNG width lives at byte offset 16 (big-endian u32 in IHDR)
		const width = buffer.readUInt32BE(16);
		const viewportMatch = tree.match(/viewport=(\d+)x/);
		const cssWidth = viewportMatch ? Number(viewportMatch[1]) : NaN;
		check(
			`screenshot width ${width} <= CSS viewport ${cssWidth} (retina descaled)`,
			Number.isFinite(cssWidth) && width <= cssWidth,
			`png=${width}px css=${cssWidth}px`
		);
		const imageTokens = Math.round((width * ((width * 9) / 16)) / 750);
		console.log(`[INFO] screenshot: ${width}px wide, ~${imageTokens} tokens (16:9 estimate)`);
	}

	//----- 8: eval_js escape hatch -----
	const evaled = textOf(
		await client.callTool({ name: 'eval_js', arguments: { script: 'return location.pathname' } })
	);
	check('eval_js returns the live pathname', evaled.includes('/performance'), evaled);

	await client.close();

	//----- 9: session trace artifacts -----
	check('expired trace session was pruned at boot', !existsSync(expiredDir), expiredDir);
	const sessions = readdirSync(TRACE_ROOT, { withFileTypes: true })
		.filter((d) => d.isDirectory())
		.map((d) => join(TRACE_ROOT, d.name));
	check('exactly one live trace session exists', sessions.length === 1, JSON.stringify(sessions));
	if (sessions.length === 1) {
		const sessionDir = sessions[0];
		const events = readFileSync(join(sessionDir, 'events.jsonl'), 'utf8').trim().split('\n');
		check(`trace recorded every tool call (${events.length} events)`, events.length >= 8, events.join('|').slice(0, 300));
		const frames = readdirSync(join(sessionDir, 'frames')).filter((f) => f.endsWith('.png'));
		check(`trace saved frames (${frames.length})`, frames.length >= 3, JSON.stringify(frames));
		const actEvent = events.map((l) => JSON.parse(l) as { tool: string; rect?: unknown; frame?: string }).find((e) => e.tool === 'act' && e.rect);
		check('a click event carries its target rect for the cursor animation', !!actEvent, events.filter((l) => l.includes('"act"')).join('|').slice(0, 300));
		const player = readFileSync(join(sessionDir, 'trace.html'), 'utf8');
		check('trace.html player has the events embedded', player.includes('"tool":"act"') && !player.includes('/*__EVENTS__*/[]'), player.slice(0, 200));
		const index = readFileSync(join(TRACE_ROOT, 'index.jsonl'), 'utf8');
		check('index.jsonl lists the live session, not the pruned one', index.includes(sessionDir) && !index.includes('expired-session'), index);
	}
}

main()
	.catch((error: unknown) => {
		failures += 1;
		console.error(`[ERROR] smoke crashed: ${String(error)}`);
	})
	.finally(() => {
		for (const child of children) child.kill('SIGINT');
		console.log(failures === 0 ? '[OK] webview-mcp smoke: ALL PASS' : `[ERROR] webview-mcp smoke: ${failures} failure(s)`);
		setTimeout(() => process.exit(failures === 0 ? 0 : 1), 1500);
	});
