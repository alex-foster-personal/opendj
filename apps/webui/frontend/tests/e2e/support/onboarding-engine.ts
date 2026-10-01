/**
 * A brand-new user's engine, one per test: the onboarding gauntlet's fixture.
 *
 * WHAT "BRAND NEW" MEANS HERE, and every clause is load bearing:
 *
 *   - a TRULY empty data dir. No `state.cli init`, no fixture builder: the
 *     packaged desktop shell creates only the directory before it spawns the
 *     engine (issue #3965), so that is what this creates.
 *   - a sandboxed HOME. apps/shared/platform_paths.py derives the rekordbox
 *     install and the default ~/Music from HOME, so a fixture that kept the
 *     real one could import the real library (see the webkit-deckload config's
 *     Wed 19 Aug 2026 incident). Here there is no rekordbox by construction.
 *   - a PACKAGED build identity. `OPENDJ_PAYLOAD_MANIFEST` is the real switch
 *     the installed launcher sets (scripts/build_engine_payload.py), and
 *     apps/engine_core/build_info.py resolves `source="payload"` from it. That
 *     is what makes GET /api/v1/setup/status answer `should_show_wizard: true`,
 *     so the wizard opens through the real `runFirstRunGate()` path and not
 *     only through the preflight empty-library fallback a checkout relies on.
 *     The manifest is written by THIS file and carries only the identity keys
 *     build_info requires; it is a test build's honest identity, not a copy of
 *     a real release's.
 *   - the SPA served by the ENGINE from apps/webui/frontend/build, exactly as
 *     the installed app serves it. No vite. The production build must exist.
 *
 * One engine per test because every case starts from an empty library and
 * most of them change it. Ports are taken from the OS (bind 0, read, close),
 * so this suite pins no fixed port and needs no host lock in CI.
 *
 * Regression lines:
 *   - if the engine reports source != "payload" then the packaged first-run
 *     trigger is not what is under test, and boot fails loudly
 *   - if the data dir holds anything before the engine starts then "fresh
 *     install" is a lie, and the fixture throws
 *   - if the engine outlives its test then the next run inherits its port
 */
import { type ChildProcess, spawn } from 'node:child_process';
import {
	chmodSync,
	existsSync,
	mkdirSync,
	mkdtempSync,
	readdirSync,
	readFileSync,
	rmSync,
	writeFileSync
} from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { guardedWebServerCommand } from './guarded-web-server';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../../../', import.meta.url));
const FRONTEND_BUILD_INDEX = fileURLToPath(new URL('../../../build/index.html', import.meta.url));

/** A cold engine boot measured 13 s on a loaded Mac (Thu 1 Oct 2026); CI hosts
 * run up to ten jobs at once, so the ceiling is generous and still finite. */
export const ENGINE_BOOT_TIMEOUT_MS = 120_000;
const HEALTH_POLL_MS = 250;

/** The identity keys apps/engine_core/build_info.py refuses to serve without. */
const TEST_BUILD_IDENTITY = {
	built_at_utc: '2026-10-01T00:00:00Z',
	git_branch: 'onboarding-gauntlet',
	git_dirty: false,
	git_sha: 'gauntlet',
	git_sha_full: 'onboarding-gauntlet-test-build',
	lane_label: 'onboarding-gauntlet'
} as const;

export interface OnboardingEngine {
	/** http://127.0.0.1:<port>, the origin the browser and the API share. */
	readonly origin: string;
	readonly dataDir: string;
	readonly home: string;
	/** Scratch space for music folders the user will point the wizard at. */
	readonly musicRoot: string;
	/** SIGKILL the whole process group: the app quitting under the user. */
	kill(): Promise<void>;
	/** Boot again on the SAME data dir and port, as a relaunch does. */
	restart(): Promise<void>;
	/** The engine's own log, for failure messages. */
	logTail(): string;
	dispose(): Promise<void>;
}

// ----- helpers ---------------------------------------------------------------

async function freeLoopbackPort(): Promise<number> {
	return new Promise((resolve, reject) => {
		const server = createServer();
		server.unref();
		server.on('error', reject);
		server.listen(0, '127.0.0.1', () => {
			const address = server.address();
			if (address === null || typeof address === 'string') {
				server.close();
				reject(new Error(`could not read a bound port from ${String(address)}`));
				return;
			}
			server.close(() => resolve(address.port));
		});
	});
}

async function sleep(ms: number): Promise<void> {
	await new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForHealth(origin: string, child: ChildProcess, log: () => string): Promise<void> {
	const deadline = Date.now() + ENGINE_BOOT_TIMEOUT_MS;
	while (Date.now() < deadline) {
		if (child.exitCode !== null || child.signalCode !== null) {
			throw new Error(
				`onboarding engine exited (${child.exitCode ?? child.signalCode}) before it was healthy:\n${log()}`
			);
		}
		try {
			const response = await fetch(`${origin}/api/v1/health`);
			if (response.ok) return;
		} catch {
			// Connection refused while the engine is still binding: keep polling.
		}
		await sleep(HEALTH_POLL_MS);
	}
	throw new Error(`onboarding engine at ${origin} not healthy after ${ENGINE_BOOT_TIMEOUT_MS} ms:\n${log()}`);
}

async function assertPackagedIdentity(origin: string): Promise<void> {
	const response = await fetch(`${origin}/api/v1/build-info`);
	const body = (await response.json()) as { source?: string };
	if (!response.ok || body.source !== 'payload') {
		throw new Error(
			`onboarding engine reports build source ${JSON.stringify(body.source)} (HTTP ${response.status}); ` +
				'the packaged first-run trigger is only under test when it is "payload"'
		);
	}
}

function engineCommand(dataDir: string, port: number): string {
	return [
		'uv run --no-sync python -m apps.engine_core serve',
		`--data-dir '${dataDir}'`,
		'--host 127.0.0.1',
		`--port ${port}`
	].join(' ');
}

function requireProductionBuild(): void {
	if (!existsSync(FRONTEND_BUILD_INDEX)) {
		throw new Error(
			`the onboarding gauntlet drives the engine-served production build, and ${FRONTEND_BUILD_INDEX} ` +
				'does not exist. Run `pnpm build` in apps/webui/frontend first.'
		);
	}
}

// ----- the fixture -----------------------------------------------------------

/** Boot a brand-new user's engine. Throws, with the engine log, if it cannot. */
export async function startOnboardingEngine(name: string): Promise<OnboardingEngine> {
	requireProductionBuild();
	const root = mkdtempSync(join(tmpdir(), `odj-onboarding-${name}-`));
	const dataDir = join(root, 'data');
	const home = join(root, 'home');
	const musicRoot = join(root, 'music');
	const manifest = join(root, 'payload', 'manifest.json');
	const logPath = join(root, 'engine.log');
	for (const dir of [dataDir, home, musicRoot, join(root, 'payload')]) mkdirSync(dir, { recursive: true });
	writeFileSync(manifest, JSON.stringify({ identity: TEST_BUILD_IDENTITY }), 'utf8');
	if (readdirSync(dataDir).length !== 0) throw new Error(`${dataDir} is not empty; not a fresh install`);

	const port = await freeLoopbackPort();
	const origin = `http://127.0.0.1:${port}`;
	let child: ChildProcess | null = null;
	let log = '';
	const logTail = (): string => log.slice(-6000);

	const boot = async (): Promise<void> => {
		const spawned = spawn('/bin/sh', ['-c', guardedWebServerCommand(`onboarding-${name}`, engineCommand(dataDir, port))], {
			cwd: REPOSITORY_ROOT,
			detached: true,
			env: {
				...process.env,
				HOME: home,
				MDT_DATA_DIR: dataDir,
				MDT_LIBRARY_MODE: 'local',
				OPENDJ_PAYLOAD_MANIFEST: manifest,
				// A packaged build with a DSN would report crashes; a test build never does.
				OPENDJ_TELEMETRY: '0',
				// The engine refuses to boot with WEB_CONCURRENCY set.
				WEB_CONCURRENCY: ''
			},
			stdio: ['ignore', 'pipe', 'pipe']
		});
		const append = (chunk: Buffer): void => {
			log += chunk.toString('utf8');
			writeFileSync(logPath, log, 'utf8');
		};
		spawned.stdout?.on('data', append);
		spawned.stderr?.on('data', append);
		child = spawned;
		await waitForHealth(origin, spawned, logTail);
		await assertPackagedIdentity(origin);
	};

	const kill = async (): Promise<void> => {
		const running = child;
		if (running === null || running.pid === undefined) return;
		if (running.exitCode === null && running.signalCode === null) {
			const exited = new Promise((resolve) => running.once('exit', resolve));
			try {
				process.kill(-running.pid, 'SIGKILL');
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error;
			}
			await exited;
		}
		child = null;
	};

	await boot();
	return {
		origin,
		dataDir,
		home,
		musicRoot,
		kill,
		restart: boot,
		logTail,
		dispose: async () => {
			await kill();
			// A denied-dir case leaves a 000 directory behind; open it so rm can walk it.
			restoreModes(root);
			rmSync(root, { recursive: true, force: true });
		}
	};
}

function restoreModes(dir: string): void {
	try {
		chmodSync(dir, 0o755);
		for (const entry of readdirSync(dir, { withFileTypes: true })) {
			if (entry.isDirectory()) restoreModes(join(dir, entry.name));
		}
	} catch {
		// Best effort before rmSync(force); a leftover tmp dir is not a test failure.
	}
}

// ----- audio the user owns -----------------------------------------------------

const SAMPLE_RATE_HZ = 44_100;

/** A real, playable 16-bit stereo PCM wav: a 440 Hz tone, `seconds` long. */
export function writeToneWav(path: string, seconds = 1): void {
	const frames = Math.round(SAMPLE_RATE_HZ * seconds);
	const channels = 2;
	const dataBytes = frames * channels * 2;
	const buffer = Buffer.alloc(44 + dataBytes);
	buffer.write('RIFF', 0, 'ascii');
	buffer.writeUInt32LE(36 + dataBytes, 4);
	buffer.write('WAVE', 8, 'ascii');
	buffer.write('fmt ', 12, 'ascii');
	buffer.writeUInt32LE(16, 16);
	buffer.writeUInt16LE(1, 20);
	buffer.writeUInt16LE(channels, 22);
	buffer.writeUInt32LE(SAMPLE_RATE_HZ, 24);
	buffer.writeUInt32LE(SAMPLE_RATE_HZ * channels * 2, 28);
	buffer.writeUInt16LE(channels * 2, 32);
	buffer.writeUInt16LE(16, 34);
	buffer.write('data', 36, 'ascii');
	buffer.writeUInt32LE(dataBytes, 40);
	for (let frame = 0; frame < frames; frame += 1) {
		const sample = Math.round(0.3 * 32767 * Math.sin((2 * Math.PI * 440 * frame) / SAMPLE_RATE_HZ));
		buffer.writeInt16LE(sample, 44 + frame * 4);
		buffer.writeInt16LE(sample, 46 + frame * 4);
	}
	writeFileSync(path, buffer);
}

/** A folder of `count` playable tracks named `<prefix> NN.wav`. Returns the paths. */
export function writeMusicFolder(dir: string, count: number, prefix = 'Gauntlet Track', seconds = 1): string[] {
	mkdirSync(dir, { recursive: true });
	const paths: string[] = [];
	for (let index = 1; index <= count; index += 1) {
		const path = join(dir, `${prefix} ${String(index).padStart(2, '0')}.wav`);
		writeToneWav(path, seconds);
		paths.push(path);
	}
	return paths;
}

/** Bytes that are named .wav and are not audio: zero-byte, prose, a bare header. */
export function writeUnplayableFiles(dir: string): string[] {
	mkdirSync(dir, { recursive: true });
	const zero = join(dir, 'Zero Byte.wav');
	const prose = join(dir, 'Not Really Audio.wav');
	const truncated = join(dir, 'Header Only.wav');
	writeFileSync(zero, Buffer.alloc(0));
	writeFileSync(prose, 'this is a text file wearing a .wav extension\n', 'utf8');
	writeToneWav(truncated, 1);
	writeFileSync(truncated, readFileSync(truncated).subarray(0, 44));
	return [zero, prose, truncated];
}
