// perf-lab A/B statistics, shared by abwin.mjs and its tests.

/** mean/sd/n over the finite numbers in xs; null when there are none. The
 * mean and sd are rounded for the report; the unrounded pair rides along as a
 * non-enumerable `exact` (absent from the JSON) for every decision, since a
 * rounded delta can clear a noise floor the true one does not. */
export const stat = (xs) => {
	const v = xs.filter((x) => typeof x === 'number' && Number.isFinite(x));
	if (!v.length) return null;
	const m = v.reduce((a, b) => a + b, 0) / v.length;
	const sd = Math.sqrt(v.reduce((a, b) => a + (b - m) ** 2, 0) / Math.max(1, v.length - 1));
	const out = { mean: +m.toFixed(2), sd: +sd.toFixed(2), n: v.length };
	Object.defineProperty(out, 'exact', { value: { mean: m, sd }, enumerable: false });
	return out;
};

/** The unrounded mean/sd of a stat() result (a plain {mean, sd} passes through). */
export const exact = (s) => s.exact ?? { mean: s.mean, sd: s.sd };

/** B - A with a Welch-style noise floor: a delta is "resolved" only when it
 * exceeds twice the combined standard error. A side with fewer than `reps`
 * samples lost a window (a process exited, a read failed), and fewer reps would
 * still yield a confident-looking delta, so that comparison is incomplete:
 * resolved is null (UNKNOWN), never true or false. A side with no samples at
 * all is incomplete too when bothSides is set (a window metric every rep
 * measures); a load stage only one side's code reports is a difference in
 * instrumentation, not a lost rep, so load stages pass bothSides false. */
export function compare(a, b, digits, reps, { bothSides = true } = {}) {
	if (!a || !b) return { A: a, B: b, delta: null, resolved: null, noise_2se: null, incomplete: bothSides };
	const [ea, eb] = [exact(a), exact(b)];
	// decided on unrounded values; only the reported fields are rounded
	const raw = eb.mean - ea.mean;
	const delta = +raw.toFixed(digits);
	if (a.n < reps || b.n < reps) return { A: a, B: b, delta, resolved: null, noise_2se: null, incomplete: true };
	if (a.n < 2 || b.n < 2) return { A: a, B: b, delta, resolved: null, noise_2se: null, incomplete: false };
	const se = Math.sqrt((ea.sd ** 2) / a.n + (eb.sd ** 2) / b.n);
	return { A: a, B: b, delta, resolved: Math.abs(raw) > 2 * se, noise_2se: +(2 * se).toFixed(2), incomplete: false };
}

/** B - A for one deck-load metric ('wall_ms' or a last_load_stages key).
 * Loads in one rep share a browser, GC state and drift, so they are not
 * independent: each rep's loads are averaged first (unrounded), and the
 * uncertainty is across reps. A rep's mean needs all `perRep` loads finite: a
 * rep missing one (a load that failed, a stage row lost for one slow track)
 * would average a different track subset than the other side, so the metric is
 * incomplete. A side whose code reports the stage on no load at all is a
 * difference in instrumentation, not a lost load (bothSides false). */
export function compareLoads(loads, key, perRep, reps) {
	const val = (l) => (key === 'wall_ms' ? l.wall_ms : l.stages?.[key]);
	let partial = false;
	const side = (s) => {
		const mine = loads.filter((l) => l.side === s);
		const repIds = [...new Set(mine.map((l) => l.rep))];
		const means = repIds.map((rep) => {
			const xs = mine.filter((l) => l.rep === rep).map(val);
			const finite = xs.filter((x) => typeof x === 'number' && Number.isFinite(x));
			if (finite.length === perRep && xs.length === perRep) return stat(finite).exact.mean;
			if (finite.length) partial = true;
			return undefined;
		});
		return stat(means);
	};
	const c = compare(side('A'), side('B'), 1, reps, { bothSides: false });
	return partial ? { ...c, resolved: null, noise_2se: null, incomplete: true } : c;
}

/** Every window metric abwin.mjs records, in report order. */
export const METRICS = ['renderer', 'gpu', 'browser', 'audio', 'chrome_total', 'engine', 'renderer_pss_mb', 'renderer_uss_mb', 'engine_pss_mb', 'js_heap_mb', 'begin_main_frames_per_s', 'main_paints_per_s', 'raf_callbacks_per_s', 'main_task_pct'];

/** Per state, per metric: compare(A, B) over the window rows. Every metric is
 * measured in every window, so one with no sample on either side stays in the
 * summary as incomplete (the run is then UNKNOWN), never silently dropped;
 * the one exception is engine PSS when no engine pid was given. */
export function summarizeStates(rows, states, reps, { engine = true } = {}) {
	const summary = {};
	for (const state of states) {
		summary[state] = {};
		for (const m of METRICS) {
			const a = stat(rows.filter((r) => r.side === 'A' && r.state === state).map((r) => r[m]));
			const b = stat(rows.filter((r) => r.side === 'B' && r.state === state).map((r) => r[m]));
			if (!a && !b && m === 'engine_pss_mb' && !engine) continue;
			summary[state][m] = compare(a, b, 2, reps);
		}
	}
	return summary;
}
