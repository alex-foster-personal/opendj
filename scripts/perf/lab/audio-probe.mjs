// perf-lab: an independent probe of what reaches each AudioContext's destination,
// shared by driver.mjs and abwin.mjs.
//
// Independent audible probe: tee every connection into the context's
// destination through an AnalyserNode the harness owns. This measures when
// non-silent samples reach the destination, by a path that shares no code with
// the app's own press-audible model, so the two can cross-check each other.
//
// Install with context.addInitScript(installAudioProbe) before any page loads.
export const installAudioProbe = () => {
	const probes = new WeakMap();
	const origConnect = AudioNode.prototype.connect;
	const origDisconnect = AudioNode.prototype.disconnect;
	const tapped = new WeakSet();
	// Every node-to-node edge the page makes (not the harness's own, which use
	// origConnect), so mixInputLevelsInPage can find the mix that feeds the
	// destination and hear each of its inputs on its own. Held weakly: the
	// leak scenario's heap snapshots must not find nodes this probe retained.
	const ins = new WeakMap();
	// the most distinct inputs each node has ever had: a deck cut from the
	// channel sum leaves it one input short, and it must still be found
	const widest = new WeakMap();
	const outs = new WeakMap();
	const inputTaps = new WeakMap();
	const refs = new WeakMap();
	const ref = (n) => {
		let r = refs.get(n);
		if (!r) refs.set(n, (r = new WeakRef(n)));
		return r;
	};
	const dropEdge = (src, dest) => {
		ins.get(dest)?.delete(ref(src));
		outs.get(src)?.delete(ref(dest));
	};
	window.__perflabAudio = { contexts: [], ins, widest, inputTaps, origConnect };
	AudioNode.prototype.connect = function (dest, ...rest) {
		const result = origConnect.call(this, dest, ...rest);
		try {
			if (dest instanceof AudioNode) {
				if (!ins.has(dest)) ins.set(dest, new Set());
				if (!outs.has(this)) outs.set(this, new Set());
				ins.get(dest).add(ref(this));
				widest.set(dest, Math.max(widest.get(dest) ?? 0, ins.get(dest).size));
				outs.get(this).add(ref(dest));
			}
			if (dest && this.context && dest === this.context.destination) {
				let probe = probes.get(this.context);
				if (!probe) {
					const analyser = this.context.createAnalyser();
					analyser.fftSize = 512;
					// a second, wider tap for the low-band EQ detector: 512 samples
					// resolve ~94 Hz bins, too coarse to separate sub-bass from mids
					const spectrum = this.context.createAnalyser();
					spectrum.fftSize = 2048;
					spectrum.smoothingTimeConstant = 0;
					probe = { analyser, spectrum, ctx: this.context, lastTsContextTime: 0, lastTsPerformanceTime: 0 };
					probes.set(this.context, probe);
					window.__perflabAudio.contexts.push(probe);
				}
				origConnect.call(this, probe.analyser);
				origConnect.call(this, probe.spectrum);
				tapped.add(this);
			}
		} catch {
			/* never let the probe break the app's graph */
		}
		return result;
	};
	// The tap must hear only what the destination hears: a node cut from the
	// destination is cut from the tap too. disconnect(outputIndex) cannot say
	// which edge it cut, so it drops the tap as well; the probe then under-hears
	// (reads silent, so the window is refused), never over-hears.
	AudioNode.prototype.disconnect = function (...args) {
		const result = origDisconnect.apply(this, args);
		try {
			const dest = args[0];
			// A disconnect that cannot name its edge drops every edge and the
			// input tap, which that call cut too: under-hear, never over-hear.
			if (args.length === 0 || typeof dest === 'number') {
				for (const r of [...(outs.get(this) ?? [])]) {
					const d = r.deref();
					if (d) dropEdge(this, d);
				}
				outs.delete(this);
				inputTaps.delete(this);
			} else if (dest instanceof AudioNode) dropEdge(this, dest);
			if (tapped.has(this) && (args.length === 0 || typeof dest === 'number' || dest === this.context.destination)) {
				const probe = probes.get(this.context);
				for (const tap of [probe.analyser, probe.spectrum]) {
					try { origDisconnect.call(this, tap); } catch { /* already gone */ }
				}
				tapped.delete(this);
			}
		} catch {
			/* never let the probe break the app's graph */
		}
		return result;
	};
};

/** RMS (dBFS) of what reached the destination over about ms, as the loudest of
 * a few analyser reads, or null when no node ever reached a destination. */
export const destinationLevelDbInPage = async (ms) => {
	const probes = window.__perflabAudio?.contexts ?? [];
	if (!probes.length) return null;
	let peak = 0;
	const t0 = performance.now();
	do {
		for (const p of probes) {
			const buf = new Float32Array(p.analyser.fftSize);
			p.analyser.getFloatTimeDomainData(buf);
			peak = Math.max(peak, Math.sqrt(buf.reduce((t, x) => t + x * x, 0) / buf.length));
		}
		await new Promise((r) => setTimeout(r, 20));
	} while (performance.now() - t0 < ms);
	return peak > 0 ? 20 * Math.log10(peak) : -Infinity;
};

/** RMS (dBFS) of each input of every mix node that feeds a destination, as
 * arrays per candidate, or null when no node ever reached a destination. A
 * mix node is any node upstream of a destination that has had exactly fanIn
 * inputs at its widest: the mixer's channel count, so the channel sum is a
 * candidate whichever decks are playing, and still is with one deck cut from
 * it (then read one input short), while a downstream fan-in whose inputs each
 * carry the whole mix (a stereo pair, a meter tap, master plus a parallel
 * bus) is not.
 * Measured on the production graph (#6531): with fanIn 2, such a pair read
 * two audible inputs with one of two decks paused. Each input is tapped once
 * by an analyser the harness owns. The destination probe hears only the
 * sum, so one deck cut while the others play passes it; one level per input
 * does not. */
export const mixInputLevelsInPage = async ([ms, fanIn]) => {
	const a = window.__perflabAudio;
	if (!a?.contexts.length) return null;
	const inputs = (n) => [...(a.ins.get(n) ?? [])].map((r) => r.deref()).filter(Boolean);
	const candidates = [];
	for (const p of a.contexts) {
		const seen = new Set([p.ctx.destination]);
		for (let frontier = [p.ctx.destination], depth = 0; frontier.length && depth < 8; depth++) {
			const next = [];
			for (const n of frontier) for (const src of inputs(n)) if (!seen.has(src)) { seen.add(src); next.push(src); }
			frontier = next;
			for (const n of next) if ((a.widest.get(n) ?? 0) === fanIn && inputs(n).length) candidates.push(inputs(n));
		}
	}
	const tap = (src) => {
		let t = a.inputTaps.get(src);
		if (!t) {
			t = src.context.createAnalyser();
			t.fftSize = 512;
			a.origConnect.call(src, t);
			a.inputTaps.set(src, t);
		}
		return t;
	};
	const taps = candidates.map((c) => c.map(tap));
	const peaks = taps.map((c) => c.map(() => 0));
	const t0 = performance.now();
	do {
		taps.forEach((c, i) => c.forEach((t, j) => {
			const buf = new Float32Array(t.fftSize);
			t.getFloatTimeDomainData(buf);
			peaks[i][j] = Math.max(peaks[i][j], Math.sqrt(buf.reduce((s, x) => s + x * x, 0) / buf.length));
		}));
		await new Promise((r) => setTimeout(r, 20));
	} while (performance.now() - t0 < ms);
	return peaks.map((c) => c.map((x) => (x > 0 ? 20 * Math.log10(x) : -Infinity)));
};

/** Starts a passive watch of decks 1..n inside the page, for the length of a
 * measured window: one timer reads every analyser once per readMs into buffers
 * allocated here, never per read, and closes a bucket every spanMs with the
 * loudest read of the destination and of each mix input, plus the fields of
 * each deck's queried state that unhealthyReason judges, read when the bucket
 * opens. Every span is judged on its own, so a dropout as long as one span
 * fails that span however loud the rest of the window was; a span is the
 * 200 ms the active check (unhealthyDecks) takes its peaks over, since a
 * single 512-sample read of real music can be momentarily quiet. No CDP round
 * trip and no sampling loop runs inside the window; stopWatchInPage hands the
 * buckets back once it ends. The mix candidates are the same nodes
 * mixInputLevelsInPage finds, re-walked for every bucket so a deck cut from
 * the mix is cut from its bucket.
 * A span's loudest read hides a dropout that covers part of it and resumes,
 * so every read is also judged on its own: it is quiet when the destination
 * reads below floorDb or fewer than n inputs of the best mix candidate reach
 * floorDb (the rules decksReasons applies to a span). A run of consecutive
 * quiet reads is carried across span edges and measured in time, from its
 * first quiet read to its latest (silentMs); each bucket keeps the longest run
 * that reached into it and its count of quiet reads (quiet), and a run of
 * SILENT_RUN_MS fails the bucket in decksReasons. */
export const startWatchInPage = ([n, spanMs, readMs, floorDb]) => {
	const a = window.__perflabAudio;
	const old = window.__perflabWatch;
	if (old) clearInterval(old.timer);
	const inputs = (node) => [...(a?.ins.get(node) ?? [])].map((r) => r.deref()).filter(Boolean);
	const bufs = new Map();
	const buf = (t) => {
		let b = bufs.get(t);
		if (!b) bufs.set(t, (b = new Float32Array(t.fftSize)));
		return b;
	};
	const rms = (t) => {
		const b = buf(t);
		t.getFloatTimeDomainData(b);
		let s = 0;
		for (let i = 0; i < b.length; i++) s += b[i] * b[i];
		return Math.sqrt(s / b.length);
	};
	const tap = (src) => {
		let t = a.inputTaps.get(src);
		if (!t) {
			t = src.context.createAnalyser();
			t.fftSize = 512;
			a.origConnect.call(src, t);
			a.inputTaps.set(src, t);
		}
		return t;
	};
	const db = (x) => (x > 0 ? 20 * Math.log10(x) : -Infinity);
	const floor = 10 ** (floorDb / 20);
	const w = { buckets: [], timer: 0 };
	let bucket = null;
	// when the current run of consecutive quiet reads began (null: no run);
	// carried from one bucket into the next
	let runStart = null;
	let runMs = 0;
	const open = () => {
		const q = window.musicDjToolsPerformance.query();
		const channels = Object.keys(q.mixer?.channels ?? {}).length;
		const taps = [];
		for (const p of a?.contexts ?? []) {
			const seen = new Set([p.ctx.destination]);
			for (let frontier = [p.ctx.destination], depth = 0; frontier.length && depth < 8; depth++) {
				const next = [];
				for (const node of frontier) for (const src of inputs(node)) if (!seen.has(src)) { seen.add(src); next.push(src); }
				frontier = next;
				for (const node of next) if ((a.widest.get(node) ?? 0) === channels && inputs(node).length) taps.push(inputs(node).map(tap));
			}
		}
		const decks = [...Array(n).keys()].map((i) => {
			const d = q.decks[i + 1];
			if (!d) return null;
			const c = d.transport_clock;
			return {
				playing: d.playing,
				processor_error: d.processor_error,
				audible: d.audible,
				transport_pending: d.transport_pending,
				transport_clock: c ? { desired_revision: c.desired_revision, presented_revision: c.presented_revision } : c
			};
		});
		bucket = { t0: Date.now(), decks, channels, taps, dest: 0, mix: taps.map((c) => c.map(() => 0)), reads: 0, quiet: 0, silentMs: runMs };
	};
	const close = () => {
		const probes = a?.contexts ?? [];
		w.buckets.push({
			t: Date.now(),
			reads: bucket.reads,
			quiet: bucket.quiet,
			silentMs: bucket.silentMs,
			decks: bucket.decks,
			channels: bucket.channels,
			dest: probes.length ? db(bucket.dest) : null,
			mix: probes.length ? bucket.mix.map((c) => c.map(db)) : null
		});
	};
	const read = () => {
		if (!bucket) open();
		let dest = 0;
		for (const p of a?.contexts ?? []) dest = Math.max(dest, rms(p.analyser));
		bucket.dest = Math.max(bucket.dest, dest);
		let heard = 0;
		bucket.taps.forEach((c, i) => {
			let k = 0;
			c.forEach((t, j) => {
				const x = rms(t);
				bucket.mix[i][j] = Math.max(bucket.mix[i][j], x);
				if (x >= floor) k++;
			});
			heard = Math.max(heard, k);
		});
		// a mix the bucket cannot measure (no probe, no candidate, too few
		// channels) already fails it as a whole; only a measured read is quiet
		const measured = (a?.contexts.length ?? 0) > 0 && bucket.taps.length > 0 && bucket.channels >= n;
		const quiet = measured && (dest < floor || heard < n);
		const now = performance.now();
		if (quiet) {
			runStart ??= now;
			runMs = now - runStart;
			bucket.quiet++;
		} else {
			runStart = null;
			runMs = 0;
		}
		bucket.silentMs = Math.max(bucket.silentMs, runMs);
		bucket.reads++;
	};
	const failed = (e) => {
		w.buckets.push({ t: Date.now(), error: String(e).slice(0, 200) });
		bucket = null;
	};
	// Each tick is the harness's work inside a measured phase, so it is
	// measured as perflab:probe, which analyze.py keeps out of the app's
	// attribution (HARNESS_PROBE).
	const tick = () => {
		const s = performance.now();
		try {
			read();
			// the next bucket opens on this same tick, so buckets tile the window
			// with no gap between them
			if (Date.now() - bucket.t0 >= spanMs) {
				close();
				open();
			}
		} catch (e) {
			failed(e);
		} finally {
			performance.measure('perflab:probe', { start: s, end: performance.now() });
		}
	};
	// stopping lands between closures: one last read, then the partial bucket
	// closes too, so the tail of the window is judged like the rest of it
	w.finish = () => {
		clearInterval(w.timer);
		try {
			read();
			close();
		} catch (e) {
			failed(e);
		}
		bucket = null;
	};
	tick();
	w.timer = setInterval(tick, readMs);
	window.__perflabWatch = w;
};

/** Ends the watch startWatchInPage began and returns its buckets, the
 * partial one it was filling included. */
export const stopWatchInPage = () => {
	const w = window.__perflabWatch;
	if (!w) return null;
	w.finish();
	window.__perflabWatch = null;
	return w.buckets;
};

/** Why fewer than n inputs of the mix are carrying audio, or null when at
 * least n are. candidates is mixInputLevelsInPage's result; the best mix node
 * is the one with the most audible inputs, so an idle cue bus beside the
 * master never stands in for it. */
export function mixInputsReason(candidates, n, floorDb = SILENT_DBFS) {
	if (!candidates) return 'no audio reaches any destination';
	if (!candidates.length) return `no mix node with one input per mixer channel feeds a destination, so each deck's output is unmeasured`;
	const best = Math.max(...candidates.map((c) => c.filter((db) => db >= floorDb).length));
	return best >= n ? null : `only ${best} of ${n} deck inputs to the mix are audible (>= ${floorDb} dBFS)`;
}

/** Below this, four playing decks are not being heard: music sits near -20 dBFS. */
export const SILENT_DBFS = -50;

/** How long a run of consecutive quiet analyser reads (see startWatchInPage)
 * may last, first quiet read to latest, before it fails its span. One read is
 * 512 samples (about 11 ms), taken every readMs (50 ms in every caller). A
 * single quiet read spans 0 ms and is tolerated: a moment of real music, a gap
 * between transients, can read quiet, and so can two reads a late timer
 * bunched a few ms apart inside one short gap. Two quiet reads a full read
 * interval apart (45 ms leaves room for timer jitter) mean the output was
 * silent at two instants 50 ms apart, and a dropout of about 110 ms or more
 * always holds two such reads (one from about 60 ms may), which is the size
 * the 200 ms span maximum misses. Reads the timer delays further apart can
 * miss a dropout of that size: the watch samples, it does not hear every
 * frame. */
export const SILENT_RUN_MS = 45;

/** Why the destination is not carrying audio, or null when it is. */
export function silentReason(levelDb, floorDb = SILENT_DBFS) {
	if (levelDb === null || levelDb === undefined) return 'no audio reaches any destination';
	if (!(levelDb >= floorDb)) return `destination silent (${Number.isFinite(levelDb) ? levelDb.toFixed(1) : levelDb} dBFS < ${floorDb})`;
	return null;
}

/** Whether a pre-press baseline was silent throughout: every one of at least
 * minSamples peaks at or under the onset detector's floor. A sound-onset
 * detector armed over output that is already sounding fires at once, so its
 * near-zero "latency" would measure nothing. Injected into the page by
 * driver.mjs, so it uses nothing from this module's scope. */
export function silentBefore(peaks, floor = 0.01, minSamples = 10) {
	return Array.isArray(peaks) && peaks.length >= minSamples && peaks.every((p) => Number.isFinite(p) && p <= floor);
}
