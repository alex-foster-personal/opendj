/**
 * Cooperative stop for analysis_source_anlz_server.py.
 *
 * `ChildProcess.kill()` is SIGTERM on POSIX but unconditional termination on
 * Windows, where the server's cleanup (removing its scratch dir) never runs.
 * POST /test/shutdown works on both. If the server does not exit cleanly, it
 * is killed and this throws, so a broken stop surfaces instead of hanging the run.
 */
const STOP_TIMEOUT_MS = 15_000;

/**
 * @param {import('node:child_process').ChildProcess} serverProcess
 * @param {string} apiBase
 * @returns {Promise<{ code: number | null, signal: NodeJS.Signals | null }>}
 */
export async function stopFixtureServer(serverProcess, apiBase) {
	if (serverProcess.exitCode !== null || serverProcess.signalCode !== null) {
		return { code: serverProcess.exitCode, signal: serverProcess.signalCode };
	}
	const exited = new Promise((resolve) =>
		serverProcess.once('exit', (code, signal) => resolve({ code, signal }))
	);
	let timer;
	const timedOut = new Promise((resolve) => {
		timer = setTimeout(() => resolve(null), STOP_TIMEOUT_MS);
	});
	try {
		const res = await fetch(`${apiBase}/test/shutdown`, { method: 'POST' });
		if (!res.ok) throw new Error(`POST /test/shutdown answered ${res.status}`);
		const outcome = await Promise.race([exited, timedOut]);
		if (outcome === null) throw new Error(`fixture server still running ${STOP_TIMEOUT_MS}ms after /test/shutdown`);
		if (outcome.code !== 0) throw new Error(`fixture server exited uncleanly: code=${outcome.code} signal=${outcome.signal}`);
		return outcome;
	} catch (error) {
		serverProcess.kill();
		throw error;
	} finally {
		clearTimeout(timer);
	}
}
