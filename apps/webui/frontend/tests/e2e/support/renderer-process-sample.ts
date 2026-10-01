import { execFileSync } from 'node:child_process';
import type { CDPSession } from '@playwright/test';

export interface ProcessInfoEntry {
	id: number;
	type: string;
}

export interface ProcessFamilyMember {
	rss_mb?: unknown;
	source?: unknown;
}

export interface ProcessTelemetryBody {
	available?: unknown;
	members?: unknown;
}

/** Every Chromium process `SystemInfo.getProcessInfo` lists: browser,
 * renderers, gpu-process and utility helpers. The page's own renderer cannot
 * be told apart from spare or extension renderers by type or order, so the
 * whole family is the measured unit, the same scope PERFMODE-15 samples. */
export function selectChromiumFamilyPids(processInfo: readonly ProcessInfoEntry[]): number[] {
	if (!processInfo.some((entry) => entry.type === 'renderer')) {
		throw new Error('CDP SystemInfo.getProcessInfo reported no renderer process');
	}
	return processInfo.map((entry) => {
		if (!Number.isInteger(entry.id) || entry.id <= 0) {
			throw new Error(`CDP SystemInfo.getProcessInfo entry has no usable pid: ${JSON.stringify(entry)}`);
		}
		return entry.id;
	});
}

/** The engine family members walked live this request (`source: 'live'`).
 *
 * The endpoint also merges `source: 'probe_log'` members: processes from the
 * native probe's last JSONL record that are not live now. That record can be
 * days old (71 h on the reference Mac, Fri 25 Sep 2026) and describes the
 * packaged app, not this browser session, so counting it adds the same stale
 * constant to Gig and Library and drags the ratio toward 1. A member with any
 * other source means an engine older than the provenance field: fail loudly
 * rather than guess from field shape. */
function liveFamilyMembers(body: ProcessTelemetryBody): ProcessFamilyMember[] {
	if (body.available !== true || !Array.isArray(body.members)) {
		throw new Error(
			'process telemetry reported available!==true; cannot measure the engine process family'
		);
	}
	const live: ProcessFamilyMember[] = [];
	for (const raw of body.members) {
		if (typeof raw !== 'object' || raw === null) {
			throw new Error(`process telemetry member is not an object: ${JSON.stringify(raw)}`);
		}
		const member = raw as ProcessFamilyMember;
		if (member.source === 'live') {
			live.push(member);
		} else if (member.source === 'probe_log') {
			continue;
		} else {
			throw new Error(
				`process telemetry member has no known source (engine predates member provenance?): ${JSON.stringify(raw)}`
			);
		}
	}
	return live;
}

/** Live engine family size (the python engine plus every descendant, e.g.
 * stem workers). A leaked worker is one extra live member; an exited process
 * still listed from the probe log is not counted at all. */
export function countLiveFamilyMembers(body: ProcessTelemetryBody): number {
	return liveFamilyMembers(body).length;
}

// ---------------------------------------------------------------------------
// This app's process family, by pid tree (PERFMODE-14, issue #3960)
// ---------------------------------------------------------------------------

export interface PsRow {
	pid: number;
	ppid: number;
	rss_kb: number;
	cpu_percent: number;
}

/** Parse `ps -Ao pid=,ppid=,rss=,%cpu=`; a malformed line throws, never drops. */
export function parsePsTable(output: string): PsRow[] {
	const rows: PsRow[] = [];
	for (const line of output.split('\n')) {
		const text = line.trim();
		if (text.length === 0) continue;
		const fields = text.split(/\s+/);
		const [pid, ppid, rssKb, cpu] = fields.map(Number);
		if (fields.length !== 4 || ![pid, ppid, rssKb, cpu].every(Number.isFinite)) {
			throw new Error(`ps returned an unparseable row: ${JSON.stringify(text)}`);
		}
		rows.push({ pid, ppid, rss_kb: rssKb, cpu_percent: cpu });
	}
	if (rows.length === 0) throw new Error('ps returned no rows');
	return rows;
}

/** The roots plus every descendant. Membership is ancestry, never a process
 * name: another worktree's `opendj-engine` is not a descendant of THIS
 * engine, so it cannot be counted in either mode, while this engine's stem
 * workers are, whatever they call themselves. A root that is not running
 * throws: a family of zero is an unmeasured family, not a small one. */
export function descendantFamilyPids(rows: readonly PsRow[], roots: readonly number[]): number[] {
	if (roots.length === 0) throw new Error('descendantFamilyPids needs at least one root pid');
	const live = new Set(rows.map((row) => row.pid));
	for (const root of roots) {
		if (!live.has(root)) throw new Error(`root pid ${root} is not running`);
	}
	const children = new Map<number, number[]>();
	for (const row of rows) {
		if (row.pid === row.ppid) continue;
		const siblings = children.get(row.ppid) ?? [];
		siblings.push(row.pid);
		children.set(row.ppid, siblings);
	}
	const family = new Set<number>(roots);
	const pending = [...roots];
	while (pending.length > 0) {
		for (const child of children.get(pending.pop() as number) ?? []) {
			if (family.has(child)) continue;
			family.add(child);
			pending.push(child);
		}
	}
	return [...family].sort((a, b) => a - b);
}

/** Parse `footprint -p <pid>` (macOS): the phys_footprint Activity Monitor shows. */
export function parseFootprintMb(output: string, pid: number): number {
	const match = output.match(/\[(\d+)\][^\n]*Footprint:\s*([\d.]+)\s*(B|KB|MB|GB)\b/);
	if (match === null) {
		throw new Error(`footprint output for pid ${pid} has no Footprint line: ${JSON.stringify(output.slice(0, 200))}`);
	}
	if (Number(match[1]) !== pid) {
		throw new Error(`footprint answered for pid ${match[1]}, asked for ${pid}`);
	}
	const value = Number(match[2]);
	const scale = { B: 1 / 1_048_576, KB: 1 / 1024, MB: 1, GB: 1024 }[match[3] as 'B' | 'KB' | 'MB' | 'GB'];
	return value * scale;
}

/** Throws unless `enginePid` -- the engine's OWN answer for its pid, from
 * `GET /api/v1/build-info` -- is one of the pids locally listening on the
 * engine's port. Sol P1/BLOCKING twice over (PR #4034, discussion_r4137393523
 * then discussion_r4137872466, which rejected a first attempt at this that
 * denied only NAMED tunnel tools such as `ssh`/`socat`: an unlisted forwarder
 * like `kubectl port-forward` or a bespoke proxy would have passed it). This
 * is a POSITIVE check instead: `--engine` reached through a local SSH or
 * other TCP forward has `lsof -iTCP:<port> -sTCP:LISTEN` return the
 * FORWARDER's pid, never the remote engine's, whatever the forwarder is
 * called -- the HTTP build-info identity checks in capture_build_identity.py
 * still pass through the tunnel (they only read the response body, which a
 * tunnel forwards correctly), so the capture would be marked measured while
 * `descendantFamilyPids` walked the forwarder's own process tree instead of
 * the engine's, silently excluding the real engine and every stem worker
 * from `engine_footprint_mb`. A truly local engine always satisfies this: it
 * binds and listens on the port itself, so its self-reported pid IS one of
 * the pids `lsof` finds there. This numeric match alone is NOT sufficient
 * for an `ssh -L` style tunnel, which binds its forwarder to loopback too --
 * see `assertLocalProcessIsTheEngine`, which this function's caller also
 * requires, for the pid-COINCIDENCE case (Sol P1/BLOCKING, discussion at
 * sha=630cec1cce, "PIDs are host-local and can coincide across machines"). */
export function assertEngineOwnsListener(pids: readonly number[], enginePid: number): void {
	if (!pids.includes(enginePid)) {
		throw new Error(
			`engine build-info reports pid ${enginePid}, but the pids listening on its port are ` +
				`[${pids.join(', ')}] -- this is exactly what a local SSH/TCP forward to a remote engine ` +
				'produces (the forwarder listens locally under its own pid while the engine answering ' +
				"HTTP requests is a different, non-local process), so the sample would walk the " +
				'forwarder\'s own tree and silently exclude the real engine and its stem workers; ' +
				'point --engine at a directly-reachable, non-tunneled origin'
		);
	}
}

/** Thrown only by `assertEnginePidPinned`, so a caller's catch-and-budget
 * loop (`dwellSample` in library-mode-perf-capture.spec.ts) can single this
 * one failure mode out from an ordinary transient sample failure (Codex
 * P1/BLOCKING, PR #4034, discussion_r4138473507): a mismatch here means the
 * engine identity itself changed, which no amount of remaining sample budget
 * can make an ordinary bad tick -- it must end the dwell outright, never be
 * absorbed as "one of the floor's tolerated misses". */
export class EnginePidMismatchError extends Error {}

/** Pure predicate: has the engine's pid changed since it was pinned? Split
 * out from `engineRootPids` so it is testable without shelling out to `lsof`
 * or fetching build-info for a real engine (same rationale as
 * `assertEngineOwnsListener` and `isEngineCommand` above).
 *
 * Sol P1/BLOCKING (PR #4034, discussion_r4138402621): `engineRootPids`
 * re-resolves the engine's pid from scratch on every call during a dwell, so
 * a same-build engine restart mid-capture passes every OTHER identity check
 * again and would otherwise swap the measured process silently, mixing
 * samples from two different process lifetimes into one ratio. Passing
 * `undefined` means "nothing pinned yet" (the first tick of a capture) and
 * never throws; every later tick must match. */
export function assertEnginePidPinned(expectedPid: number | undefined, currentPid: number): void {
	if (expectedPid !== undefined && currentPid !== expectedPid) {
		throw new EnginePidMismatchError(
			`engine pid changed mid-capture: pinned ${expectedPid}, now ${currentPid} -- the ` +
				'engine likely restarted during the dwell, which would mix samples from two ' +
				'different process lifetimes into one ratio'
		);
	}
}

/** Pure predicate over a `ps` command-line string: does it name engine code
 * (`apps.engine_core` or the packaged `opendj-engine` launcher)? Split out
 * from `assertLocalProcessIsTheEngine` so it is testable without shelling
 * out to `ps` for a real pid.
 *
 * Sol P1/BLOCKING (PR #4034, discussion at sha=09612c7a6f): an earlier
 * version matched `/engine_core|opendj-engine/` as a SUBSTRING anywhere in
 * the command, which an `opendj-engine-proxy` binary, or a forwarding
 * command whose ARGUMENT happens to contain the engine's name (e.g.
 * `ssh -L 8686:opendj-engine-host:8686 user@opendj-engine-host`), would
 * satisfy without being the engine at all -- exactly the PID-coincidence
 * case this function exists to close. This checks STRUCTURE instead of a
 * substring: either argv[0]'s basename is EXACTLY `opendj-engine` (the
 * packaged launcher, never a name that merely contains it), or the tokens
 * contain the exact adjacent pair `-m apps.engine_core` (a real Python
 * module invocation, never a hostname or path that happens to embed those
 * words), AND argv[0] is actually a Python-like interpreter (`python`,
 * `python3`, `python3.x`, or `uv`, which this repo's own invocations all
 * use -- see justfile and scripts/build_engine_payload.py).
 *
 * Sol P1/BLOCKING (PR #4034, discussion_r4148668247): the adjacent-pair
 * search used to scan EVERY token, so `ssh -L 8686:remote:8686 host python
 * -m apps.engine_core` -- an ssh forwarder whose REMOTE command happens to
 * be the real engine invocation, not merely a lookalike name -- still
 * passed, because nothing required argv[0] (here `ssh`) to be the thing
 * actually running that module. Requiring argv[0] to be a Python-like
 * interpreter closes this the same way the `opendj-engine` basename check
 * already closes the packaged-launcher case. */
export function isEngineCommand(command: string): boolean {
	const tokens = command.trim().split(/\s+/).filter(Boolean);
	if (tokens.length === 0) return false;
	const argv0Basename = basename(tokens[0]);
	if (argv0Basename === 'opendj-engine') return true;
	if (PYTHON_BASENAME.test(argv0Basename)) return pythonRunsEngineModule(tokens.slice(1));
	if (argv0Basename === 'uv') return uvRunsEngineModule(tokens.slice(1));
	return false;
}

const PYTHON_BASENAME = /^python3?(\.\d+)?$/;
const ENGINE_MODULE = 'apps.engine_core';
/** Python interpreter options that consume the NEXT token as their value. */
const PYTHON_VALUE_OPTIONS = new Set(['-X', '-W', '--check-hash-based-pycs']);

function basename(path: string): string {
	return path.split('/').pop() ?? '';
}

/** Does this Python argv (after argv[0]) run `apps.engine_core` as its program?
 *
 * Sol P1/BLOCKING (PR #4540): scanning every later token for `-m
 * apps.engine_core` let `python proxy.py -m apps.engine_core` pass, where
 * Python runs `proxy.py` and the module pair is only that script's argument.
 * Python's program is the FIRST non-option token, so walk the interpreter's
 * own options and decide there: `-m apps.engine_core` is the engine, and a
 * script path, `-c`, `-`, or `--` is something else. Anything unrecognized
 * fails closed (false), which makes the capture refuse rather than sample a
 * process it could not identify. */
function pythonRunsEngineModule(args: string[]): boolean {
	for (let i = 0; i < args.length; i++) {
		const arg = args[i];
		if (arg === '-m') return args[i + 1] === ENGINE_MODULE;
		if (PYTHON_VALUE_OPTIONS.has(arg)) {
			i++;
			continue;
		}
		if (arg === '-c' || arg === '-' || arg === '--' || !arg.startsWith('-')) return false;
	}
	return false;
}

/** Does `uv run ...` (argv after `uv`) run `apps.engine_core`?
 *
 * Only flag-only uv options are skipped: a value-taking option leaves its
 * value where the command belongs, which is not a Python interpreter, so it
 * fails closed. */
function uvRunsEngineModule(args: string[]): boolean {
	if (args[0] !== 'run') return false;
	for (let i = 1; i < args.length; i++) {
		const arg = args[i];
		if (arg === '-m' || arg === '--module') return args[i + 1] === ENGINE_MODULE;
		if (arg === '--') return PYTHON_BASENAME.test(basename(args[i + 1] ?? '')) && pythonRunsEngineModule(args.slice(i + 2));
		if (arg.startsWith('-')) continue;
		return PYTHON_BASENAME.test(basename(arg)) && pythonRunsEngineModule(args.slice(i + 1));
	}
	return false;
}

/** Throws unless the LOCAL process at `pid` is actually running engine code.
 * Sol P1/BLOCKING (PR #4034, sha=630cec1cce): a numeric pid match alone
 * (`assertEngineOwnsListener`) is not sufficient, because pids are
 * host-local integers with no cross-host uniqueness guarantee -- an
 * `ssh -L <port>:remote:<port>` tunnel binds its FORWARDER to loopback (so
 * `assertLoopbackOrigin` alone does not catch it either), and if the remote
 * engine's self-reported pid happens to numerically equal the local
 * forwarder's own pid, `assertEngineOwnsListener` passes on that
 * coincidence. This is a second, independent, POSITIVE identity check
 * (not a forwarder deny-list, which discussion_r4137872466 already
 * rejected): it asks what the verified-local pid's OWN command line says
 * it is, and an ssh/socat/kubectl/nc/bespoke-proxy forwarder can never
 * satisfy it, because none of them run engine code. */
export function assertLocalProcessIsTheEngine(pid: number): void {
	const command = execFileSync('ps', ['-ww', '-o', 'command=', '-p', String(pid)], {
		encoding: 'utf-8'
	}).trim();
	if (!isEngineCommand(command)) {
		throw new Error(
			`pid ${pid} passed the build-info pid check but its own command line is ` +
				`'${command || '(no such local process)'}', which does not name the engine -- this is ` +
				"exactly what a local port-forward (ssh -L, kubectl port-forward, socat, a bespoke proxy) " +
				"produces when its own pid happens to numerically coincide with the remote engine's " +
				'self-reported pid; point --engine at a directly-reachable, non-tunneled origin'
		);
	}
}

/** The engine's own `pid` field from `GET /api/v1/build-info`
 * (apps/engine_core/build_info.py), stamped at route construction from this
 * serving process's `os.getpid()`. A response missing or misshaping it is
 * refused rather than skipping the tunnel check it exists for. */
async function fetchEnginePid(apiBase: string): Promise<number> {
	const response = await fetch(`${apiBase.replace(/\/$/, '')}/api/v1/build-info`);
	if (!response.ok) {
		throw new Error(`build-info at ${apiBase} returned HTTP ${response.status}`);
	}
	const body: unknown = await response.json();
	const pid = (body as { pid?: unknown }).pid;
	if (!Number.isInteger(pid) || (pid as number) <= 0) {
		throw new Error(`build-info at ${apiBase} has no usable 'pid': ${JSON.stringify(body)}`);
	}
	return pid as number;
}

const _LOOPBACK_HOSTNAMES = new Set(['localhost', '127.0.0.1', '[::1]']);

/** Throws unless `apiBase`'s hostname is loopback. Codex P1/BLOCKING (PR
 * #4034, discussion_r4137567525): `engineRootPids` runs `lsof` against a
 * PORT NUMBER on the capture machine regardless of what host `--engine`
 * names. If `--engine` points at a remote host, and a local process
 * (possibly another engine) also happens to be listening on that same
 * port locally, `assertEngineOwnsListener` only compares PID NUMBERS --
 * the remote engine's self-reported pid could coincidentally equal the
 * unrelated local listener's pid, which would pass and silently sample
 * the wrong process tree. Rather than rely on that coincidence being rare
 * enough, refuse the case outright: this capture only ever needs to run
 * against a directly-reachable local engine, so a non-loopback origin is
 * refused up front, before lsof or fetch runs at all. */
export function assertLoopbackOrigin(apiBase: string): void {
	const hostname = new URL(apiBase).hostname;
	if (!_LOOPBACK_HOSTNAMES.has(hostname)) {
		throw new Error(
			`API base ${apiBase} is not loopback (hostname '${hostname}') -- this capture only samples ` +
				"processes LOCAL to the machine it runs on, so a remote --engine can never be measured " +
				'correctly: lsof only sees local listeners on the port number, which could coincidentally ' +
				"belong to an unrelated local process rather than the named remote engine. Run the capture " +
				'on the same host as the engine, reachable at localhost/127.0.0.1.'
		);
	}
}

/** The engine's own root pid, cross-checked against the API origin's port.
 * Sol P1/BLOCKING (PR #4034, discussion_r4137960871): `lsof -iTCP:<port>`
 * matches every LOCAL listener on that port number regardless of which
 * address it is bound to, so if an unrelated process happens to listen on
 * the same port on a different interface, returning lsof's whole pid list
 * as roots would silently fold that unrelated process's tree into the
 * footprint/CPU denominators alongside the real engine. The fix is to use
 * lsof only to prove the engine's self-reported pid -- see
 * `assertEngineOwnsListener` -- is LOCAL (a tunneled `--engine` can never
 * satisfy that, see the two prior rounds on this function), then return
 * ONLY that one verified pid as the root. Every other pid lsof happened to
 * list is irrelevant once the engine's own identity is confirmed local.
 *
 * `expectedPid`, when given, pins the engine identity across an entire
 * capture (Sol P1/BLOCKING, PR #4034, discussion_r4138402621): this function
 * re-resolves the engine's pid from scratch on every call, so an engine
 * restart mid-dwell (same build, new pid) would otherwise pass every
 * identity check again and silently swap the measured process, mixing
 * samples from two different process lifetimes behind a ratio that still
 * looks like one continuous capture. Callers pin the pid from their first
 * successful sample and pass it on every later call; a mismatch here means
 * the engine changed and must fail the tick loud, not average it in. */
export async function engineRootPids(apiBase: string, expectedPid?: number): Promise<number[]> {
	assertLoopbackOrigin(apiBase);
	const port = new URL(apiBase).port;
	if (port === '') throw new Error(`API base ${apiBase} has no explicit port`);
	const output = execFileSync('lsof', ['-nP', `-iTCP:${port}`, '-sTCP:LISTEN', '-t'], {
		encoding: 'utf-8'
	});
	const pids = [...new Set(output.split(/\s+/).filter(Boolean).map(Number))];
	if (pids.length === 0 || !pids.every((pid) => Number.isInteger(pid) && pid > 0)) {
		throw new Error(`no process is listening on ${apiBase}: ${JSON.stringify(output)}`);
	}
	const enginePid = await fetchEnginePid(apiBase);
	assertEngineOwnsListener(pids, enginePid);
	assertLocalProcessIsTheEngine(enginePid);
	assertEnginePidPinned(expectedPid, enginePid);
	return [enginePid];
}

function readPsTable(): PsRow[] {
	return parsePsTable(execFileSync('ps', ['-Ao', 'pid=,ppid=,rss=,%cpu='], { encoding: 'utf-8' }));
}

function readFootprintMb(pid: number): number {
	return parseFootprintMb(
		execFileSync('footprint', ['-p', String(pid)], { encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'] }),
		pid
	);
}

export interface FamilySample {
	/** phys_footprint (MB), Chromium family plus engine family: the KPI value. */
	footprint_mb: number;
	/** Resident set (MB) of the same pids, kept for continuity with older rows. */
	rss_mb: number;
	/** `ps` %CPU of the same pids. */
	cpu_percent: number;
	chromium_footprint_mb: number;
	engine_footprint_mb: number;
	engine_pids: number[];
	/** phys_footprint (MB) summed per Chromium process type, for attribution. */
	chromium_by_type_mb: Record<string, number>;
}

/**
 * One sample of THIS app's process family, attributed by pid tree:
 * - every Chromium process CDP's browser target lists (this Playwright
 *   browser only; stands in for the packaged WKWebView, which Playwright's
 *   Chrome is not -- see PR #3679 review),
 * - PLUS the engine listening on `apiBase` and every process it spawned
 *   (stem workers), found by ancestry, so another worktree's engine is never
 *   counted (issue #3960 measured about 108 MB of foreign engines in BOTH
 *   modes when membership was by `opendj-*` name).
 *
 * Footprint is phys_footprint via `footprint -p`, not RSS. On a host under
 * memory pressure the kernel compresses and swaps a big idle Gig renderer, so
 * RSS stops being its footprint: on silver, Sat 26 Sep 2026, with 8.7 GB of
 * swap in use, the Gig renderer read 449 MB RSS against 1701 MB
 * phys_footprint. PERFMODE-15's sampler already reads phys_footprint for the
 * same reason (PR #3676 review).
 *
 * `cdp` must be a BROWSER-target session (`browser.newBrowserCDPSession()`):
 * a page session rejects SystemInfo.getProcessInfo on every call. Every read
 * throws rather than degrading: a partial-family sample is exactly the
 * false-PASS class this KPI exists to catch, so callers apply a bounded
 * minimum-sample-count check across the dwell instead of tolerating gaps.
 *
 * `expectedEnginePid`, when given, is forwarded to `engineRootPids` to pin
 * the engine's identity across an entire capture (Sol P1/BLOCKING, PR #4034,
 * discussion_r4138402621) -- see its docstring for why re-resolving fresh on
 * every tick, alone, cannot catch a same-build engine restart mid-dwell.
 */
export async function sampleProcessFamilyFootprint(
	cdp: CDPSession,
	apiBase: string,
	expectedEnginePid?: number
): Promise<FamilySample> {
	const response = await cdp.send('SystemInfo.getProcessInfo');
	const processInfo = (response as { processInfo?: ProcessInfoEntry[] }).processInfo;
	if (!Array.isArray(processInfo)) {
		throw new Error('CDP SystemInfo.getProcessInfo returned no processInfo array');
	}
	const chromiumPids = selectChromiumFamilyPids(processInfo);
	const rows = readPsTable();
	const enginePids = descendantFamilyPids(rows, await engineRootPids(apiBase, expectedEnginePid));
	const overlap = enginePids.filter((pid) => chromiumPids.includes(pid));
	if (overlap.length > 0) throw new Error(`pids counted in both families: ${overlap.join(',')}`);
	const byPid = new Map(rows.map((row) => [row.pid, row]));
	let rssMb = 0;
	let cpuPercent = 0;
	for (const pid of [...chromiumPids, ...enginePids]) {
		const row = byPid.get(pid);
		if (row === undefined) throw new Error(`pid ${pid} exited mid-sample`);
		rssMb += row.rss_kb / 1024;
		cpuPercent += row.cpu_percent;
	}
	const chromiumByTypeMb: Record<string, number> = {};
	let chromiumFootprintMb = 0;
	for (const entry of processInfo) {
		const mb = readFootprintMb(entry.id);
		chromiumFootprintMb += mb;
		chromiumByTypeMb[entry.type] = (chromiumByTypeMb[entry.type] ?? 0) + mb;
	}
	const engineFootprintMb = enginePids.reduce((sum, pid) => sum + readFootprintMb(pid), 0);
	return {
		footprint_mb: chromiumFootprintMb + engineFootprintMb,
		rss_mb: rssMb,
		cpu_percent: cpuPercent,
		chromium_footprint_mb: chromiumFootprintMb,
		engine_footprint_mb: engineFootprintMb,
		engine_pids: enginePids,
		chromium_by_type_mb: chromiumByTypeMb
	};
}
