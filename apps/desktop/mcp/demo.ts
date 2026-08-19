/**
 * Live demo driver for webview-mcp: boots the fixture engine + REAL debug
 * shell (the window appears on screen), then drives it through the MCP tools
 * with pauses so a human can watch. Saves screenshots to the scratchpad.
 * Run from apps/desktop so SDK imports resolve.
 */
import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const DESKTOP_ROOT = join(new URL('.', import.meta.url).pathname, '..');
const REPOSITORY_ROOT = join(DESKTOP_ROOT, '..', '..');
const APP_BINARY = join(DESKTOP_ROOT, 'src-tauri', 'target', 'debug', 'opendj-desktop');
const DATA_DIR = join(DESKTOP_ROOT, 'mcp', '.smoke-data');
const SANDBOX_HOME = join(DATA_DIR, 'sandbox-home');
const OUT_DIR = process.env.DEMO_OUT_DIR ?? '.';
const ENGINE_PORT = 8698;
const WEBDRIVER_PORT = 4456;
const ENGINE_ORIGIN = `http://127.0.0.1:${ENGINE_PORT}`;

const children: ChildProcess[] = [];
const pause = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

function say(step: string): void {
	console.log(`\n===== ${step} =====`);
}

function textOf(result: unknown): string {
	return (result as { content: Array<{ type: string; text?: string }> }).content
		.filter((c) => c.type === 'text')
		.map((c) => c.text)
		.join('\n');
}

async function waitForHttp(url: string, timeoutMs: number): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		try {
			if ((await fetch(url)).ok) return;
		} catch {
			/* not up yet */
		}
		await pause(400);
	}
	throw new Error(`${url} never became ready`);
}

async function main(): Promise<void> {
	if (!existsSync(APP_BINARY)) throw new Error(`debug shell missing: ${APP_BINARY}`);
	if (!existsSync(DATA_DIR)) throw new Error(`fixture data missing (run just webview-mcp-smoke once): ${DATA_DIR}`);
	mkdirSync(SANDBOX_HOME, { recursive: true });

	say('booting fixture engine + real debug shell (watch for the window)');
	const engine = spawn(
		'uv',
		['run', '--no-sync', 'python', '-m', 'apps.engine_core', 'serve', '--data-dir', DATA_DIR, '--host', '127.0.0.1', '--port', String(ENGINE_PORT)],
		{ cwd: REPOSITORY_ROOT, stdio: 'ignore', env: { ...process.env, MDT_DATA_DIR: DATA_DIR, WEB_CONCURRENCY: '', HOME: SANDBOX_HOME } }
	);
	children.push(engine);
	await waitForHttp(`${ENGINE_ORIGIN}/api/v1/health`, 120_000);

	const shell = spawn(APP_BINARY, [], {
		env: { ...process.env, OPENDJ_ENGINE_ORIGIN: ENGINE_ORIGIN, TAURI_WEBDRIVER_PORT: String(WEBDRIVER_PORT), HOME: SANDBOX_HOME },
		stdio: 'ignore'
	});
	children.push(shell);
	await waitForHttp(`http://127.0.0.1:${WEBDRIVER_PORT}/status`, 60_000);

	const transport = new StdioClientTransport({
		command: 'pnpm',
		args: ['exec', 'tsx', join(DESKTOP_ROOT, 'mcp', 'webview-mcp.ts')],
		cwd: DESKTOP_ROOT,
		env: {
			...process.env,
			MDT_WEBVIEW_MCP_WEBDRIVER: `http://127.0.0.1:${WEBDRIVER_PORT}`,
			MDT_WEBVIEW_MCP_ENGINE: ENGINE_ORIGIN
		}
	});
	const client = new Client({ name: 'webview-mcp-demo', version: '0.0.1' });
	await client.connect(transport);
	await pause(4000);

	say('tool: navigate /performance');
	console.log(textOf(await client.callTool({ name: 'navigate', arguments: { url: '/performance' } })));
	await pause(2500);

	say('tool: app_state (engine JSON, the cheapest observation)');
	console.log(textOf(await client.callTool({ name: 'app_state', arguments: {} })).slice(0, 400));

	say('tool: ui_tree (first 40 lines)');
	const tree = textOf(await client.callTool({ name: 'ui_tree', arguments: {} }));
	console.log(tree.split('\n').slice(0, 40).join('\n'));
	console.log(`... (${tree.split('\n').length} lines, ${tree.length} chars, ~${Math.round(tree.length / 4)} tokens)`);
	await pause(2000);

	say('tool: act chord Meta+, -- WATCH THE WINDOW: Settings opens');
	console.log(textOf(await client.callTool({ name: 'act', arguments: { kind: 'chord', text: 'Meta+,' } })));
	await pause(3000);

	say('tool: screenshot (settings open) -> demo-settings-open.png');
	const shot1 = (await client.callTool({ name: 'screenshot', arguments: {} })) as {
		content: Array<{ type: string; data?: string }>;
	};
	const img1 = shot1.content.find((c) => c.type === 'image');
	if (!img1?.data) throw new Error('screenshot returned no image');
	writeFileSync(join(OUT_DIR, 'demo-settings-open.png'), Buffer.from(img1.data, 'base64'));
	console.log('saved');
	await pause(1500);

	say('tool: act chord Escape -- WATCH: Settings closes');
	console.log(textOf(await client.callTool({ name: 'act', arguments: { kind: 'chord', text: 'Escape' } })));
	await pause(2500);

	say('tool: ui_tree scoped to the track browser, then act click on a real row by @ref');
	const scoped = textOf(
		await client.callTool({ name: 'ui_tree', arguments: { selector: '[data-testid="track-table"], main, body' } })
	);
	const versionMatch = scoped.match(/\[v(\d+)\]/);
	const rowMatch = scoped.split('\n').find((l) => l.includes('track-row') && l.includes('@'));
	console.log(rowMatch ? `target row line: ${rowMatch.trim()}` : '(no track-row ref found; clicking first button ref instead)');
	const anyRef = (rowMatch ?? scoped.split('\n').find((l) => / button .*@e\d+/.test(l)) ?? '').match(/@(e\d+)/);
	if (anyRef && versionMatch) {
		console.log(
			textOf(
				await client.callTool({
					name: 'act',
					arguments: { kind: 'click', ref: anyRef[1], tree_version: Number(versionMatch[1]) }
				})
			)
		);
	}
	await pause(2500);

	say('tool: screenshot (final state) -> demo-final.png');
	const shot2 = (await client.callTool({ name: 'screenshot', arguments: {} })) as {
		content: Array<{ type: string; data?: string }>;
	};
	const img2 = shot2.content.find((c) => c.type === 'image');
	if (!img2?.data) throw new Error('second screenshot returned no image');
	writeFileSync(join(OUT_DIR, 'demo-final.png'), Buffer.from(img2.data, 'base64'));
	console.log('saved');

	say('demo complete -- window stays up 8s more, then everything shuts down');
	await pause(8000);
	await client.close();
}

main()
	.then(() => console.log('\n[OK] demo finished cleanly'))
	.catch((error: unknown) => {
		console.error(`\n[ERROR] demo failed: ${String(error)}`);
		process.exitCode = 1;
	})
	.finally(() => {
		for (const child of children) child.kill('SIGINT');
		setTimeout(() => process.exit(process.exitCode ?? 0), 1500);
	});
