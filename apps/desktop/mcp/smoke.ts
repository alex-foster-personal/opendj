/**
 * End-to-end smoke for webview-mcp: real fixture engine, real DEBUG shell,
 * real MCP stdio transport. No mocks anywhere. Exits nonzero on ANY failure.
 *
 * Ports: engine 8698, WebDriver 4456, CDP 9336 -- deliberately distinct from
 * every reserved suite port (tier-1 8690, tier-2 8691/4455, boot test 8697,
 * lanes).
 *
 * Shells: the Tauri debug shell over WebDriver by default; `--shell electron`
 * runs the same checks against the unpackaged Electron shell over CDP (Linux
 * needs a display, e.g. `xvfb-run -a`).
 *
 * Acceptance tests:
 *   [if] the tree lacks the app's known landmarks [then ⛔️] fail, not warn
 *   [if] act(chord Meta+,) does not surface the Settings dialog [then ⛔️] fail
 *   [if] act() with a stale ref does NOT error [then ⛔️] fail (staleness is
 *        load-bearing: agents must be forced to re-observe)
 *   [if] screenshot width exceeds the CSS viewport width [then ⛔️] fail
 *        (retina 2x leaking through = mushy text for vision models)
 */
import { createRequire } from 'node:module';
import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { signalGroup, TaskkillUnavailableError } from '../electron/src/engine';
import { chromiumSandboxArgs } from './chromium-sandbox';
import { groupMembers, sweepGroup, type Member } from './process-group';
import { resolveTool } from './resolve-tool';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const DESKTOP_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPOSITORY_ROOT = join(DESKTOP_ROOT, '..', '..');
// Cargo names the binary after the target platform's executable convention (Codex on #6830).
const APP_BINARY = join(
	DESKTOP_ROOT,
	'src-tauri',
	'target',
	'debug',
	process.platform === 'win32' ? 'opendj-desktop.exe' : 'opendj-desktop'
);
const ELECTRON_DIR = join(DESKTOP_ROOT, 'electron');
const SHELL: 'tauri' | 'electron' = process.argv.includes('--shell')
	? (process.argv[process.argv.indexOf('--shell') + 1] as 'tauri' | 'electron')
	: 'tauri';
if (SHELL !== 'tauri' && SHELL !== 'electron') throw new Error(`--shell must be tauri or electron, got ${String(SHELL)}`);
const FIXTURE_BUILDER = join(
	REPOSITORY_ROOT,
	'apps/webui/frontend/tests/e2e/support/deckload_fixture.py'
);

const ENGINE_PORT = 8698;
const WEBDRIVER_PORT = 4456;
const CDP_PORT = 9336;
const CDP_ORIGIN = `http://127.0.0.1:${CDP_PORT}`;
const ENGINE_ORIGIN = `http://127.0.0.1:${ENGINE_PORT}`;
const WEBDRIVER_ORIGIN = `http://127.0.0.1:${WEBDRIVER_PORT}`;
const DATA_DIR = join(DESKTOP_ROOT, 'mcp', '.smoke-data');
const SANDBOX_HOME = join(DATA_DIR, 'sandbox-home');
const TRACE_ROOT = join(DATA_DIR, 'traces');

const children: ChildProcess[] = [];
let failures = 0;
/** Each long-lived child leads its own process group on POSIX, so stopping it reaches its
 * helpers too; Windows has no groups and `signalGroup` walks the tree with taskkill /T. */
const OWN_GROUP = { detached: process.platform !== 'win32' } as const;
const STOP_GRACE_MS = 5_000;
/** How often a stopping child's group is re-listed while its leader is still unreaped. */
const REMEMBER_EVERY_MS = 25;
const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** Stop one child and everything it spawned, and wait until the leader has exited. */
async function stopTree(child: ChildProcess): Promise<void> {
	const pid = child.pid;
	if (pid === undefined) return;
	const exited = new Promise<void>((resolve) => {
		if (child.exitCode !== null || child.signalCode !== null) resolve();
		else child.once('exit', () => resolve());
	});
	if (process.platform === 'win32' && (child.exitCode !== null || child.signalCode !== null)) {
		// taskkill /T walks the tree from a live leader only, and Windows has no group to
		// sweep, so a helper the leader left behind cannot be found: say so, never pass.
		failures += 1;
		console.error(
			`[ERROR] child ${pid} exited before cleanup; any helper it left may still hold ports or ${DATA_DIR}`
		);
		return;
	}
	// Taken while the leader is alive, so the sweep below can prove the group is still
	// this child's once its leader has gone (Codex on #6830). A leader that already
	// exited leaves nothing to take it from, and the sweep then touches nothing.
	// A failed listing must not stop the cleanup below; the sweep then proves nothing.
	const remembered: Member[] = [];
	/** Adds the group's current members, but only while the leader is unreaped: until Node
	 * reaps it (never during this synchronous call) its pid, and so the group id, is ours. */
	const remember = (): void => {
		if (process.platform === 'win32' || child.exitCode !== null || child.signalCode !== null) return;
		try {
			for (const member of groupMembers(pid)) {
				if (!remembered.some((r) => r.pid === member.pid && r.started === member.started)) remembered.push(member);
			}
		} catch (error) {
			console.error(`[WARN] could not list child ${pid}'s group while stopping it: ${(error as Error).message}`);
		}
	};
	/** Signal the tree; null when Windows has no taskkill.exe to ask, after counting that as a
	 * failure and killing the leader alone so the other children are still stopped (Codex on #6830). */
	const signalTree = (signal: NodeJS.Signals): boolean | null => {
		try {
			return signalGroup(pid, signal);
		} catch (error) {
			if (!(error instanceof TaskkillUnavailableError)) throw error;
			failures += 1;
			console.error(`[ERROR] ${error.message}; killing child ${pid} alone, any helper may still hold ports or ${DATA_DIR}`);
			child.kill('SIGKILL');
			return null;
		}
	};
	remember();
	if (child.exitCode === null && child.signalCode === null) {
		// On Windows a false means taskkill /T never reached the tree, so the leader exiting
		// on its own would prove nothing about its helpers: go straight to the forced kill.
		const termed = signalTree('SIGTERM');
		if (termed === null) {
			await Promise.race([exited, sleep(STOP_GRACE_MS)]);
			return;
		}
		const asked = termed || process.platform !== 'win32';
		// The group is re-listed while the leader winds down, so a helper it starts after
		// the first listing (the backend's analysis runs) is remembered too, and can prove
		// the group ours if it outlives its leader.
		let graceful = false;
		if (asked) {
			let done = false;
			void exited.then(() => {
				done = true;
			});
			const deadline = Date.now() + STOP_GRACE_MS;
			while (!done && Date.now() < deadline) {
				remember();
				await Promise.race([exited, sleep(REMEMBER_EVERY_MS)]);
			}
			graceful = done;
		}
		if (!graceful) {
			console.error(
				asked
					? `[WARN] child ${pid} outlived SIGTERM by ${STOP_GRACE_MS} ms; killing its tree`
					: `[WARN] taskkill /T could not ask child ${pid}'s tree to stop; killing it`
			);
			const killedTree = signalTree('SIGKILL');
			if (killedTree === null) {
				await Promise.race([exited, sleep(STOP_GRACE_MS)]);
				return;
			}
			if (!killedTree) {
				console.error(`[ERROR] could not kill child ${pid}'s tree`);
				if (process.platform === 'win32') {
					// Nothing else can find the helpers on Windows: never call this clean. The
					// leader is still this run's handle, so it is killed and waited on (Sol P1 on #6830).
					failures += 1;
					child.kill('SIGKILL');
					await Promise.race([exited, sleep(STOP_GRACE_MS)]);
					return;
				}
			}
			const killed = await Promise.race([exited.then(() => true), sleep(STOP_GRACE_MS).then(() => false)]);
			if (!killed) {
				failures += 1;
				console.error(`[ERROR] child ${pid} is still alive ${STOP_GRACE_MS} ms after SIGKILL; giving up on it`);
				return;
			}
		}
	}
	// A helper can outlive its leader (an Electron renderer or GPU process); sweep the
	// group, but only while a remembered member proves its id was never reused.
	if (process.platform === 'win32') return;
	let sweep: Awaited<ReturnType<typeof sweepGroup>>;
	try {
		sweep = await sweepGroup(pid, remembered);
	} catch (error) {
		failures += 1;
		console.error(`[ERROR] could not sweep child ${pid}'s group: ${(error as Error).message}`);
		return;
	}
	if ('survived' in sweep) {
		failures += 1;
		console.error(`[ERROR] child ${pid}'s group still has ${sweep.survived.join(', ')} after SIGKILL; giving up on it`);
	} else if ('swept' in sweep && sweep.swept > 0) {
		console.error(`[WARN] child ${pid}'s group still had members after it exited; killed them`);
	} else if ('leftAlone' in sweep) {
		failures += 1;
		console.error(
			`[ERROR] group ${pid} still has process(es) ${sweep.leftAlone.join(', ')} that this run cannot prove ` +
				`are child ${pid}'s (its id may have been reused), so they were left alone; any helper among them may ` +
				`still hold ports or ${DATA_DIR}`
		);
	}
}

function withoutPageSeams(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
	const {
		MDT_WEBVIEW_MCP_WEBDRIVER: _webdriver,
		MDT_WEBVIEW_MCP_CDP: _cdp,
		MDT_WEBVIEW_MCP_APP: _app,
		...rest
	} = env;
	return rest;
}

/** Children whose stop has begun, so a second pass never signals one twice. */
const stopping = new Set<ChildProcess>();

/** Stop every child, including any listed while an earlier pass was still stopping others. */
async function stopAll(): Promise<void> {
	for (;;) {
		const pending = children.filter((child) => !stopping.has(child));
		if (pending.length === 0) return;
		for (const child of pending) stopping.add(child);
		await Promise.all(pending.map(stopTree));
	}
}

/**
 * The one way out: stop every tree, then exit with `signalCode` (130/143) or, with
 * none, the run's verdict. The signal handler and `main`'s own end share it, so the
 * second caller waits for the first cleanup rather than exiting under it with a
 * different status (Codex on #6830).
 */
let shutdown: Promise<never> | undefined;
/** The first signal's status, read only at exit: a signal that lands while `main`'s own
 * cleanup is already running still decides the status (Codex on #6830). */
let signalStatus: number | undefined;
function finish(signalCode?: number): Promise<never> {
	signalStatus ??= signalCode;
	shutdown ??= (async (): Promise<never> => {
		await stopAll();
		if (signalStatus === undefined) {
			console.log(
				failures === 0
					? `[OK] webview-mcp smoke (${SHELL}): ALL PASS`
					: `[ERROR] webview-mcp smoke (${SHELL}): ${failures} failure(s)`
			);
		}
		process.exit(signalStatus ?? (failures === 0 ? 0 : 1));
	})();
	return shutdown;
}

/**
 * Await a short-lived setup child (the fixture build, the shell's tsc), listed in
 * `children` while it runs so a SIGINT or SIGTERM to the harness stops it too rather
 * than orphaning it (Codex on #6830). Resolves to its exit code, or a description of
 * the signal that ended it.
 */
async function runSetup(child: ChildProcess): Promise<number | string> {
	children.push(child);
	let outcome: number | string;
	try {
		outcome = await new Promise((resolve, reject) => {
			child.once('error', reject);
			child.once('exit', (code, signal) => resolve(code ?? `on ${signal}`));
		});
	} finally {
		const at = children.indexOf(child);
		if (at >= 0) children.splice(at, 1);
	}
	// Stopping: never let main go on to start the next child.
	if (shutdown) throw new Error('the smoke is stopping');
	return outcome;
}

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
			// Bounded per request: a listener that accepts and never answers must not outlast the deadline.
			const response = await fetch(url, { signal: AbortSignal.timeout(5_000) });
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
	if (SHELL === 'tauri' && !existsSync(APP_BINARY)) {
		throw new Error(`debug shell missing: ${APP_BINARY} (cd apps/desktop/src-tauri && cargo build)`);
	}

	//----- boot engine over a fresh fixture -----
	rmSync(DATA_DIR, { recursive: true, force: true });
	mkdirSync(SANDBOX_HOME, { recursive: true });
	// Resolved once, so the fixture builder and the engine run the same uv (Codex on #6830).
	const uv = resolveTool('uv');
	const buildCode = await runSetup(
		spawn(uv, ['run', '--no-sync', 'python', FIXTURE_BUILDER, '--data-dir', DATA_DIR], {
			cwd: REPOSITORY_ROOT,
			stdio: 'inherit',
			...OWN_GROUP
		})
	);
	if (buildCode !== 0) throw new Error(`fixture builder exited ${buildCode}`);

	const engine = spawn(
		uv,
		['run', '--no-sync', 'python', '-m', 'apps.engine_core', 'serve', '--data-dir', DATA_DIR, '--host', '127.0.0.1', '--port', String(ENGINE_PORT)],
		{
			cwd: REPOSITORY_ROOT,
			stdio: 'inherit',
			env: { ...process.env, MDT_DATA_DIR: DATA_DIR, WEB_CONCURRENCY: '', HOME: SANDBOX_HOME },
			...OWN_GROUP
		}
	);
	children.push(engine);
	await waitForHttp(`${ENGINE_ORIGIN}/api/v1/health`, 120_000);

	if (SHELL === 'tauri') {
		//----- boot the debug shell with the embedded WebDriver -----
		const shell = spawn(APP_BINARY, [], {
			env: {
				...process.env,
				OPENDJ_ENGINE_ORIGIN: ENGINE_ORIGIN,
				TAURI_WEBDRIVER_PORT: String(WEBDRIVER_PORT),
				HOME: SANDBOX_HOME
			},
			stdio: 'ignore',
			...OWN_GROUP
		});
		children.push(shell);
		await waitForHttp(`${WEBDRIVER_ORIGIN}/status`, 60_000);
	} else {
		//----- boot the unpackaged Electron shell with its CDP port -----
		const electronRequire = createRequire(join(ELECTRON_DIR, 'package.json'));
		const tscCode = await runSetup(
			spawn(process.execPath, [electronRequire.resolve('typescript/bin/tsc'), '-p', 'tsconfig.json'], {
				cwd: ELECTRON_DIR,
				stdio: 'inherit',
				...OWN_GROUP
			})
		);
		if (tscCode !== 0) throw new Error(`electron shell build exited ${tscCode}`);
		const electronBinary = electronRequire('electron') as string;
		const sandboxArgs = chromiumSandboxArgs();
		const shell = spawn(
			electronBinary,
			[...sandboxArgs, `--user-data-dir=${join(DATA_DIR, 'electron-user-data')}`, ELECTRON_DIR],
			{
				env: {
					...process.env,
					// `.test.` marks a harness-spawned tree the orphan reaper may collect.
					AF_SERVICE_ID: 'com.opendj.test.electron-smoke',
					OPENDJ_ENGINE_ORIGIN: ENGINE_ORIGIN,
					OPENDJ_REMOTE_DEBUGGING_PORT: String(CDP_PORT),
					OPENDJ_SHELL_DATA_DIR: join(DATA_DIR, 'electron-shell')
				},
				stdio: 'ignore',
				...OWN_GROUP
			}
		);
		children.push(shell);
		await waitForHttp(`${CDP_ORIGIN}/json/version`, 60_000);
	}

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
		// This Node runs tsx's own CLI: no package-manager shim to find on PATH (Codex on #6830).
		command: process.execPath,
		args: [createRequire(join(DESKTOP_ROOT, 'package.json')).resolve('tsx/cli'), join(DESKTOP_ROOT, 'mcp', 'webview-mcp.ts')],
		cwd: DESKTOP_ROOT,
		env: {
			// Drop both page seams first: an inherited one would make webview-mcp refuse to start.
			...withoutPageSeams(process.env),
			...(SHELL === 'tauri' ? { MDT_WEBVIEW_MCP_WEBDRIVER: WEBDRIVER_ORIGIN } : { MDT_WEBVIEW_MCP_CDP: CDP_ORIGIN }),
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

// The children lead their own groups, so a terminal Ctrl-C or a CI timeout's SIGTERM no longer
// reaches them: forward either one.
for (const [signal, code] of [['SIGINT', 130], ['SIGTERM', 143]] as const) {
	process.once(signal, () => {
		console.error(`[ERROR] smoke got ${signal}; stopping its process trees`);
		void finish(code);
	});
}

main()
	.catch((error: unknown) => {
		failures += 1;
		console.error(`[ERROR] smoke crashed: ${String(error)}`);
	})
	.finally(() => finish());
