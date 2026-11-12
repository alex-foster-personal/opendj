// Readiness-aware launcher for scripts/testing/midi_maps_test_server.py, the
// throwaway REAL backend that midi-installed-maps.test.mjs and
// midi-panel-resync.test.mjs drive (AGENTS.md "No mocks and locked real
// fixtures").
//
// Issue #4332: on a loaded runner (megamac-vm-4, Mon 28 Sep 2026) both suites
// failed with "did not report LISTENING within 20000ms". A single wall-clock
// timer cannot tell a slow cold import from a hung or dead server, and its
// error said nothing about which one it was. This launcher waits for REAL
// readiness instead:
//
//   1. The server prints `BOOT <stage> <ms>` per startup stage, then
//      `LISTENING <port>`. Every line is proof of progress, so the stall
//      budget restarts on each one: a server that keeps moving is waited for,
//      a server that stops moving fails after `stallMs`.
//   2. A hard ceiling (`totalMs`) still bounds the whole wait, so a dead
//      server can never hang the suite (the #4332 disposition of Thu 1 Oct
//      2026: keep the bounded fail-fast signal, never just a bigger number).
//   3. An exit (any code, or a signal) or a spawn error before LISTENING
//      fails at once instead of waiting out the budget.
//   4. LISTENING alone is not the contract the tests need: one real GET of
//      the midi maps route must answer 200 before the port is handed out.
//
// Every failure names what was observed: the reason, how long it waited and
// against which budget, the boot stages reached with their timings, the
// process exit code or signal, and the stderr tail.

import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';

import { fixtureServerLaunch } from '../../scripts/fixture-server.mjs';

/**
 * @param {string} repoRoot
 * @param {readonly string[]} [extraArgs]
 * @param {NodeJS.ProcessEnv} [env]
 */
export function midiMapsFixtureLaunch(repoRoot, extraArgs = [], env = {}) {
	return fixtureServerLaunch({
		repoRoot,
		pythonTarget: 'scripts.testing.midi_maps_test_server',
		pythonTargetIsModule: true,
		extraArgs: ['--port', '0', ...extraArgs],
		env
	});
}

// Startup tolerance only, not a product latency contract. stallMs keeps the
// 60 s that 53a531e27 measured as enough for the slowest single stage (the
// cold import of the app under host contention); totalMs keeps the whole
// before() hook inside the runner's 120 s --test-timeout with room for the
// vite SSR load that follows.
export const DEFAULT_BUDGET = Object.freeze({ stallMs: 60_000, totalMs: 90_000, probeMs: 10_000 });

const PROBE_PATH = '/api/v1/midi/maps';
const STDERR_TAIL_LINES = 20;
const EXIT_DRAIN_MS = 500;

function _tail(text, lines) {
	const all = text.split(/\r?\n/).filter((line) => line.length > 0);
	return all.slice(-lines).join('\n');
}

/**
 * Spawn the server and resolve once it is really ready.
 *
 * @param {object} options
 * @param {string} options.cwd repository root the server module resolves from
 * @param {string} [options.command] program to run (default `uv`)
 * @param {readonly string[]} [options.args] its arguments
 * @param {number} [options.stallMs] longest wait with no new boot line
 * @param {number} [options.totalMs] hard ceiling for the whole start
 * @param {number} [options.probeMs] budget for the post-LISTENING HTTP probe
 * @returns {Promise<{proc: import('node:child_process').ChildProcess, port: number,
 *   origin: string, boot: {stage: string, ms: number}[], readyAfterMs: number}>}
 */
export function startMidiMapsTestServer({
	cwd,
	command,
	args,
	env,
	stallMs = DEFAULT_BUDGET.stallMs,
	totalMs = DEFAULT_BUDGET.totalMs,
	probeMs = DEFAULT_BUDGET.probeMs
}) {
	const launch =
		command !== undefined || args !== undefined
			? { command, args: args ?? [], env: { ...process.env, ...env }, cwd }
			: midiMapsFixtureLaunch(cwd, [], env);
	const resolvedCommand = launch.command;
	const resolvedArgs = launch.args;
	const resolvedEnv = launch.env;
	const resolvedCwd = launch.cwd;
	return new Promise((resolveReady, reject) => {
		const startedAt = Date.now();
		const boot = [];
		let stderr = '';
		let settled = false;
		let stallTimer = null;
		let totalTimer = null;
		let exitInfo = null;

		const proc = spawn(resolvedCommand, resolvedArgs, {
			cwd: resolvedCwd,
			env: resolvedEnv,
			stdio: ['ignore', 'pipe', 'pipe']
		});
		proc.stderr.on('data', (chunk) => {
			stderr += String(chunk);
		});

		const describe = (reason) => {
			const waited = Date.now() - startedAt;
			const stages = boot.length
				? boot.map(({ stage, ms }) => `${stage}@${ms}ms`).join(', ')
				: 'none (no BOOT line seen)';
			const process_ = exitInfo
				? `exited with code ${exitInfo.code} signal ${exitInfo.signal}`
				: `still running as pid ${proc.pid}, killed`;
			const tail = _tail(stderr, STDERR_TAIL_LINES) || '(empty)';
			return (
				`midi_maps_test_server not ready: ${reason}. ` +
				`Waited ${waited}ms (stall budget ${stallMs}ms, ceiling ${totalMs}ms). ` +
				`Boot stages reached: ${stages}. Process: ${process_}. ` +
				`Command: ${resolvedCommand} ${resolvedArgs.join(' ')} (cwd ${resolvedCwd}).\n` +
				`stderr tail (last ${STDERR_TAIL_LINES} lines):\n${tail}`
			);
		};

		const fail = (reason) => {
			if (settled) return;
			settled = true;
			clearTimeout(stallTimer);
			clearTimeout(totalTimer);
			if (!exitInfo) proc.kill();
			reject(new Error(describe(reason)));
		};

		const armStall = () => {
			clearTimeout(stallTimer);
			const last = boot.length
				? `after stage '${boot[boot.length - 1].stage}'`
				: 'before the first BOOT line';
			stallTimer = setTimeout(() => fail(`no startup progress for ${stallMs}ms ${last}`), stallMs);
		};

		totalTimer = setTimeout(() => fail(`not listening within the ${totalMs}ms ceiling`), totalMs);
		armStall();

		proc.on('error', (error) => fail(`could not spawn '${command}': ${error.message}`));
		proc.on('exit', (code, signal) => {
			exitInfo = { code, signal };
			// 'exit' can fire before the last stderr chunk is read; give the
			// pipes a moment to drain ('close') so the tail holds the traceback.
			const report = () => fail('the process exited before it was ready');
			const drained = setTimeout(report, EXIT_DRAIN_MS);
			proc.once('close', () => {
				clearTimeout(drained);
				report();
			});
		});

		const lines = createInterface({ input: proc.stdout });
		lines.on('line', (line) => {
			if (settled) return;
			const bootMatch = /^BOOT (\S+) (\d+)$/.exec(line);
			if (bootMatch) {
				boot.push({ stage: bootMatch[1], ms: Number(bootMatch[2]) });
				armStall();
				return;
			}
			const listening = /^LISTENING (\d+)$/.exec(line);
			if (!listening) return;
			boot.push({ stage: 'listening', ms: Date.now() - startedAt });
			clearTimeout(stallTimer);
			const port = Number(listening[1]);
			const origin = `http://127.0.0.1:${port}`;
			_probe(origin, probeMs).then(
				() => {
					if (settled) return;
					settled = true;
					clearTimeout(totalTimer);
					lines.close();
					proc.stdout.resume(); // keep draining so the pipe never back-pressures the server
					resolveReady({ proc, port, origin, boot, readyAfterMs: Date.now() - startedAt });
				},
				(error) =>
					fail(`listening on ${port} but ${PROBE_PATH} never answered 200 (${error.message})`)
			);
		});
	});
}

/** One real GET of the route the suites use, retried until 200 or probeMs. */
async function _probe(origin, probeMs) {
	const deadline = Date.now() + probeMs;
	let last = 'no attempt';
	while (Date.now() < deadline) {
		try {
			const response = await fetch(`${origin}${PROBE_PATH}`, {
				signal: AbortSignal.timeout(2_000)
			});
			if (response.ok) return;
			last = `HTTP ${response.status}`;
		} catch (error) {
			last = error.cause?.message ?? error.message;
		}
		await new Promise((r) => setTimeout(r, 100));
	}
	throw new Error(`last result ${last} after ${probeMs}ms`);
}
