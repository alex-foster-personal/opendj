// perf-lab /proc CPU and memory window for one browser's process family plus
// an engine's, used by abwin.mjs. Linux-only: it reads /proc directly.
import fs from 'node:fs';

const HZ = 100; // USER_HZ on Linux
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------ /proc readers --
const procTicks = (pid) => {
	try { const s = fs.readFileSync(`/proc/${pid}/stat`, 'utf8'); const f = s.slice(s.lastIndexOf(')') + 2).split(' '); return Number(f[11]) + Number(f[12]); } catch { return null; }
};
// CPU of children this process has already reaped (cutime + cstime): it grows
// when a descendant exits inside a window, whether or not a listing ever saw it.
const procChildTicks = (pid) => {
	try { const s = fs.readFileSync(`/proc/${pid}/stat`, 'utf8'); const f = s.slice(s.lastIndexOf(')') + 2).split(' '); return Number(f[13]) + Number(f[14]); } catch { return null; }
};
const cmdline = (pid) => { try { return fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').replace(/\0/g, ' '); } catch { return ''; } };
const ppid = (pid) => { try { const s = fs.readFileSync(`/proc/${pid}/stat`, 'utf8'); return Number(s.slice(s.lastIndexOf(')') + 2).split(' ')[1]); } catch { return null; } };
function descendants(root) {
	const all = fs.readdirSync('/proc').filter((d) => /^\d+$/.test(d)).map(Number);
	const kids = new Map();
	for (const p of all) { const pp = ppid(p); if (pp !== null) (kids.get(pp) ?? kids.set(pp, []).get(pp)).push(p); }
	const out = [], stack = [root];
	while (stack.length) { const p = stack.pop(); out.push(p); stack.push(...(kids.get(p) ?? [])); }
	return out;
}
function roleOf(pid) {
	return roleOfCmdline(cmdline(pid));
}
// The role of a browser-family process from its command line. The main
// process has no --type; its binary may be chrome, chromium or Chromium.
export function roleOfCmdline(c) {
	if (/--type=renderer/.test(c)) return /--extension-process/.test(c) ? 'other' : 'renderer';
	if (/--type=gpu-process/.test(c)) return 'gpu';
	if (/AudioService/.test(c)) return 'audio';
	if (/--type=/.test(c)) return 'other';
	return /chrom(e|ium)/i.test(c) ? 'browser' : 'other';
}
function smaps(pid) {
	try {
		const t = fs.readFileSync(`/proc/${pid}/smaps_rollup`, 'utf8');
		const kb = (k) => Number((t.match(new RegExp(`^${k}:\\s+(\\d+)`, 'm')) ?? [])[1] ?? NaN);
		return { pss_mb: kb('Pss') / 1024, uss_mb: (kb('Private_Clean') + kb('Private_Dirty')) / 1024, rss_mb: kb('Rss') / 1024 };
	} catch { return null; }
}
// Start time in clock ticks since boot (/proc/<pid>/stat field 22): with the
// pid it names one process, so a pid recycled mid-window is not mistaken for it.
const procStart = (pid) => {
	try { const s = fs.readFileSync(`/proc/${pid}/stat`, 'utf8'); return Number(s.slice(s.lastIndexOf(')') + 2).split(' ')[19]); } catch { return null; }
};
const uptimeTicks = () => Number(fs.readFileSync('/proc/uptime', 'utf8').split(' ')[0]) * HZ;
/** Summed PSS and USS (MB) of the renderer pids, or null for both when there
 * is no renderer or any one of them could not be read (it exited, or its
 * smaps_rollup was unreadable): a partial total would read as a saving. */
export function rendererMemory(pids) {
	const m = pids.map(smaps);
	if (!m.length || m.some((x) => x === null || !Number.isFinite(x.pss_mb) || !Number.isFinite(x.uss_mb))) {
		return { renderer_pss_mb: null, renderer_uss_mb: null };
	}
	return {
		renderer_pss_mb: +m.reduce((t, x) => t + x.pss_mb, 0).toFixed(1),
		renderer_uss_mb: +m.reduce((t, x) => t + x.uss_mb, 0).toFixed(1),
	};
}

/** Summed PSS (MB) of pids, or null when there are none or any read is lost:
 * a partial total would read as a saving. */
export function familyPss(pids) {
	const m = pids.map(smaps);
	if (!m.length || m.some((x) => x === null || !Number.isFinite(x.pss_mb))) return null;
	return +m.reduce((t, x) => t + x.pss_mb, 0).toFixed(1);
}

export async function cpuWindow(browserPid, enginePid, secs, { pollMs = 250 } = {}) {
	// Both families are read at the start AND the end, and polled in between: a
	// helper born inside the window is counted from zero, and a process that
	// exits inside it takes its CPU with it, so that window's CPU is unmeasurable
	// and is nulled, never undercounted (ab.py and stat() then see fewer samples,
	// not a saving). A descendant that is born AND exits between two looks is
	// still caught: its parent's reaped-children ticks grow, or a poll saw it.
	const family = () => [
		...descendants(browserPid).filter((p) => p !== process.pid).map((p) => [p, roleOf(p)]),
		...(enginePid ? descendants(Number(enginePid)).map((p) => [p, 'engine']) : []),
	];
	const t0Ticks = uptimeTicks();
	const start = new Map(family().map(([p, role]) => [p, { role, start: procStart(p), ticks: procTicks(p), child: procChildTicks(p) }]));
	const polled = new Set();
	const deadline = Date.now() + secs * 1000;
	while (Date.now() < deadline) {
		await sleep(Math.min(pollMs, Math.max(0, deadline - Date.now())));
		for (const [p] of family()) polled.add(`${p}:${procStart(p)}`);
	}
	const end = family();
	// per second of the window as it really ran (polling stretches it a little)
	const elapsed = Math.max(secs, (uptimeTicks() - t0Ticks) / HZ);
	const out = { renderer: 0, gpu: 0, browser: 0, audio: 0, other: 0, engine: 0 };
	const lifecycle = { born: 0, exited: 0, unmatched: 0, reaped: 0, unseen: 0 };
	const seen = new Set();
	const endKeys = new Set();
	for (const [p, role] of end) {
		const s0 = start.get(p), st = procStart(p), b = procTicks(p), ch = procChildTicks(p);
		if (st === null || b === null) continue; // exited between listing and reading: counted below
		endKeys.add(`${p}:${st}`);
		if (s0 && s0.start === st && s0.ticks !== null) {
			out[role] += b - s0.ticks; seen.add(p);
			if (ch === null || s0.child === null || ch > s0.child) lifecycle.reaped++;
		} else if (st >= t0Ticks - 1) {
			out[role] += b; lifecycle.born++;
			if (ch === null || ch > 0) lifecycle.reaped++;
		} else lifecycle.unmatched++; // older than the window yet missing from its start: no baseline
	}
	for (const p of start.keys()) if (!seen.has(p)) lifecycle.exited++;
	const startKeys = new Set([...start].map(([p, v]) => `${p}:${v.start}`));
	for (const k of polled) if (!startKeys.has(k) && !endKeys.has(k) && !k.endsWith(':null')) lifecycle.unseen++;
	const lost = lifecycle.exited + lifecycle.unmatched + lifecycle.reaped + lifecycle.unseen > 0;
	for (const k of Object.keys(out)) out[k] = lost ? null : +((out[k] / HZ / elapsed) * 100).toFixed(2);
	out.chrome_total = lost ? null : +(out.renderer + out.gpu + out.browser + out.audio + out.other).toFixed(2);
	out.cpu_lifecycle = lifecycle;
	const fam = end.filter(([, r]) => r !== 'engine');
	const eng = end.filter(([, r]) => r === 'engine').map(([p]) => p);
	Object.assign(out, rendererMemory(fam.filter(([, r]) => r === 'renderer').map(([p]) => p)));
	// the whole engine family: the supervisor may run the native engine as a
	// child, whose memory is the part a native change moves
	if (eng.length) out.engine_pss_mb = familyPss(eng);
	return out;
}

/** Waits until the browser's and engine's process families have stayed the same
 * set for stableMs (a new browser starts and retires helper processes for a
 * while), so a window does not open on start-up churn it would then have to
 * void. Returns true once stable, false at the timeout; the window that follows
 * still nulls its CPU if churn happens inside it. */
export async function awaitStableFamily(browserPid, enginePid, { stableMs = 2000, timeoutMs = 20_000, pollMs = 250 } = {}) {
	const key = () => [
		...descendants(browserPid).filter((p) => p !== process.pid),
		...(enginePid ? descendants(Number(enginePid)) : []),
	].map((p) => `${p}:${procStart(p)}`).sort().join(',');
	const t0 = Date.now();
	let last = key(), since = Date.now();
	while (Date.now() - t0 < timeoutMs) {
		await sleep(pollMs);
		const now = key();
		if (now !== last) { last = now; since = Date.now(); }
		else if (Date.now() - since >= stableMs) return true;
	}
	return false;
}
