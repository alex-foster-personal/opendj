// Extra engine processes the payload declares, such as the Rust audio engine.
//
// The contract is the Python engine's (engine.ts `spawnProcess`): launched with
// --data-dir/--host/--port, its own process group, logs pumped into
// logs/<name>.log, ready when GET /api/v1/health answers 200. Each ready
// origin is injected into every page as the global the manifest names.
//
// payload/sidecars.json:
//   { "sidecars": [ { "name": "audio-engine",
//                     "launcher": "bin/odj-audio-engine",
//                     "origin_global": "OPENDJ_AUDIO_ENGINE_ORIGIN",
//                     "required": false } ] }
//
// An optional sidecar that fails is logged and its global left absent; it
// never blocks launch. A required one fails the launch like the engine does.

import * as fs from 'node:fs';
import * as path from 'node:path';

import { BOOT_TIMEOUT_MS, type Engine, EngineError, freeLoopbackPort, spawnProcess } from './engine';
import { appendShellLog } from './shell-log';

export const SIDECAR_MANIFEST = 'sidecars.json';
const NAME_PATTERN = /^[a-z][a-z0-9-]{0,31}$/;
const GLOBAL_PATTERN = /^OPENDJ_[A-Z0-9_]{1,48}$/;

export interface SidecarSpec {
	name: string;
	launcher: string;
	originGlobal: string;
	required: boolean;
}

export interface RunningSidecar {
	spec: SidecarSpec;
	engine: Engine;
}

/**
 * Parse and validate the manifest. Absent file: no sidecars. Present but
 * malformed: an error, never a silent skip, because a payload that meant to
 * ship an audio engine and did not is a broken build.
 */
export function readSidecarManifest(payloadDir: string): SidecarSpec[] {
	const file = path.join(payloadDir, SIDECAR_MANIFEST);
	let raw: string;
	try {
		raw = fs.readFileSync(file, 'utf8');
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === 'ENOENT') return [];
		throw new EngineError('Open DJ could not read its sidecar manifest.', `${file}: ${(error as Error).message}`);
	}
	const bad = (why: string): EngineError => new EngineError('This Open DJ build has a broken sidecar manifest.', `${file}: ${why}`);
	let parsed: unknown;
	try {
		parsed = JSON.parse(raw);
	} catch (error) {
		throw bad(`not JSON (${(error as Error).message})`);
	}
	const list = (parsed as { sidecars?: unknown } | null)?.sidecars;
	if (!Array.isArray(list)) throw bad('expected {"sidecars": [...]}');
	const seen = new Set<string>();
	return list.map((entry: unknown, index) => {
		const e = (entry ?? {}) as Record<string, unknown>;
		const { name, launcher, origin_global: originGlobal, required } = e;
		if (typeof name !== 'string' || !NAME_PATTERN.test(name)) throw bad(`sidecars[${index}].name must match ${NAME_PATTERN}`);
		if (name === 'engine' || seen.has(name)) throw bad(`sidecars[${index}].name "${name}" is taken`);
		seen.add(name);
		if (typeof launcher !== 'string' || launcher === '' || path.isAbsolute(launcher) || launcher.split(/[\\/]/).includes('..')) {
			throw bad(`sidecars[${index}].launcher must be a relative path inside the payload`);
		}
		if (typeof originGlobal !== 'string' || !GLOBAL_PATTERN.test(originGlobal) || originGlobal === 'OPENDJ_ENGINE_ORIGIN') {
			throw bad(`sidecars[${index}].origin_global must match ${GLOBAL_PATTERN} and not be OPENDJ_ENGINE_ORIGIN`);
		}
		if (required !== undefined && typeof required !== 'boolean') throw bad(`sidecars[${index}].required must be a boolean`);
		return { name, launcher, originGlobal, required: required === true };
	});
}

/** Start every sidecar; returns the ones that came up. */
export async function startSidecars(
	payloadDir: string,
	dataDir: string,
	logDir: string,
	specs: SidecarSpec[],
	timeoutMs = BOOT_TIMEOUT_MS
): Promise<RunningSidecar[]> {
	const running: RunningSidecar[] = [];
	for (const spec of specs) {
		try {
			const port = await freeLoopbackPort();
			const engine = spawnProcess({
				name: spec.name,
				launcher: path.join(payloadDir, spec.launcher),
				dataDir,
				logPath: path.join(logDir, `${spec.name}.log`),
				port,
				env: { OPENDJ_PARENT_PID: String(process.pid) }
			});
			try {
				await engine.waitUntilHealthy(timeoutMs);
			} catch (error) {
				await engine.shutdown();
				throw error;
			}
			appendShellLog('sidecar', `${spec.name} healthy at ${engine.origin()} (pid ${engine.pid})`);
			void engine.waitReap().then((exit) => {
				appendShellLog('WARN', `sidecar ${spec.name} exited (code ${String(exit.code)}, signal ${String(exit.signal)})`);
			});
			running.push({ spec, engine });
		} catch (error) {
			if (spec.required) {
				await stopSidecars(running);
				throw error;
			}
			appendShellLog('WARN', `optional sidecar ${spec.name} did not start: ${(error as Error).message}`);
		}
	}
	return running;
}

export async function stopSidecars(running: RunningSidecar[]): Promise<void> {
	await Promise.all(running.map((sidecar) => sidecar.engine.shutdown()));
}

/** `{ OPENDJ_AUDIO_ENGINE_ORIGIN: "http://127.0.0.1:NNNN" }` for the live ones. */
export function sidecarGlobals(running: RunningSidecar[]): Record<string, string> {
	const globals: Record<string, string> = {};
	for (const sidecar of running) {
		if (sidecar.engine.tryReap() === null) globals[sidecar.spec.originGlobal] = sidecar.engine.origin();
	}
	return globals;
}
