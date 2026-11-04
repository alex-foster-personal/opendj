/**
 * Lifecycle helpers for the REAL fixture servers some unit tests spawn
 * (`uv run --no-sync python ...`): a bounded wait for the server's
 * ready line, and a stop that ends the whole process tree.
 *
 * Why a tree kill: on Windows `uv.exe` cannot exec, so the python server is a
 * GRANDCHILD of the test process. `ChildProcess#kill()` is TerminateProcess on
 * uv.exe alone; the server survives, keeps the stdout pipe it inherited open,
 * the test process's event loop never drains, and `node --test` (which has no
 * file-level timeout) waits on it forever. Measured Tue 22 Sep 2026 on
 * bifrost2: midi-installed-maps.test.mjs passed all 9 tests at +11s and was
 * still in flight at +55s with the orphan (`python.exe -m
 * scripts.testing.midi_maps_test_server`, parent pid already dead) holding
 * its pipe. Under `--test-concurrency=4` the four fixture files each pin a
 * slot the same way, so the whole suite stalls mid-run with no error.
 *
 * Why a bounded ready wait: a `before()` hook has no default timeout, so a
 * fixture that never prints its ready line is the same indefinite stall, and
 * `--test-timeout` does not reach hooks.
 */
import { spawnSync } from 'node:child_process';
import { createInterface } from 'node:readline';
import { join } from 'node:path';

/** taskkill exit status for "no such process": the tree is already gone,
 * which is the postcondition this function exists to establish. */
const TASKKILL_NOT_FOUND = 128;
/** Absolute path, so a `taskkill` earlier on PATH cannot shadow the system one. */
const TASKKILL_PATH = join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'taskkill.exe');

/** Stop `proc` and, on Windows, every descendant it spawned. No-op for a
 * process that has already exited. Throws when the tree could not be ended,
 * so a test never reports green over a fixture it left running. */
export function killProcessTree(proc) {
	if (!proc || proc.exitCode !== null || proc.signalCode !== null) return;
	if (process.platform !== 'win32') {
		// POSIX: `kill()` already ends the fixture (the Linux and macOS unit
		// jobs stay green with it); only Windows leaves a grandchild behind.
		proc.kill();
		return;
	}
	const result = spawnSync(TASKKILL_PATH, ['/PID', String(proc.pid), '/T', '/F'], {
		encoding: 'utf8',
		stdio: 'pipe'
	});
	if (result.status !== 0 && result.status !== TASKKILL_NOT_FOUND) {
		throw new Error(
			`taskkill /T /F failed for pid ${proc.pid} (exit ${result.status}): ${result.stderr || result.stdout}`
		);
	}
}

/** Resolve with the first capture group of the first stdout line matching
 * `pattern` (e.g. `/^READY (\d+)$/` -> the port), as a Number. Rejects if the
 * process exits first or prints nothing matching within `timeoutMs`; in the
 * timeout case the tree is killed before rejecting so the file cannot stall. */
export function waitForReadyLine(proc, pattern, { timeoutMs, label }) {
	return new Promise((resolve, reject) => {
		const lines = createInterface({ input: proc.stdout });
		const timer = setTimeout(() => {
			killProcessTree(proc);
			reject(new Error(`${label} did not print ${pattern} within ${timeoutMs}ms`));
		}, timeoutMs);
		proc.once('exit', (code, signal) => {
			clearTimeout(timer);
			reject(new Error(`${label} exited early (${code ?? signal})`));
		});
		lines.on('line', (line) => {
			const match = pattern.exec(line);
			if (match) {
				clearTimeout(timer);
				resolve(Number(match[1]));
			}
		});
	});
}
