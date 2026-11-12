// perf-lab: is a deck really producing presented audio? Shared by driver.mjs and
// abwin.mjs, because a silent deck reads as a CPU saving in either.
//
// `playing` with no processor error is not enough: a suspended AudioContext, a
// transport still settling, or a schedule revision the output has not presented
// yet all leave a deck "playing" while nothing is heard. Healthy is the same
// contract the performance presets publish ready against: audible, transport
// settled, and the acknowledged schedule revision presented; and the harness's
// own destination probe must hear it.
import { SILENT_DBFS, SILENT_RUN_MS, destinationLevelDbInPage, mixInputLevelsInPage, mixInputsReason, silentReason, startWatchInPage, stopWatchInPage } from './audio-probe.mjs';

/** Why a deck's queried state is not healthy playing audio, or null when it is. */
export function unhealthyReason(dk) {
	if (!dk) return 'no deck state';
	const why = [];
	if (!dk.playing) why.push('not playing');
	if (dk.processor_error) why.push(`processor_error=${dk.processor_error}`);
	if (dk.audible !== true) why.push('not audible');
	if (dk.transport_pending !== false) why.push('transport pending');
	const c = dk.transport_clock;
	if (!c || c.desired_revision !== c.presented_revision) {
		why.push(`schedule revision ${c?.desired_revision} not presented (output at ${c?.presented_revision})`);
	}
	return why.length ? why.join(', ') : null;
}

/** Why a loaded deck's queried state does not hold `expected` ready on a live
 * processor, or null when it does. A processor that dies clears the deck and
 * records processor_error, but the next load clears that error again, so a
 * phase that loads repeatedly must check this after every load window, not
 * only at its end. */
export function loadedReason(dk, expected) {
	if (!dk) return 'no deck state';
	const why = [];
	if (dk.stable_id !== expected) why.push(`holds ${dk.stable_id ?? 'nothing'}, expected ${expected}`);
	if (dk.processor_error) why.push(`processor_error=${dk.processor_error}`);
	if (dk.transport_pending !== false) why.push('transport pending');
	return why.length ? why.join(', ') : null;
}

/** Reasons decks 1..expected.length do not each hold expected[deck - 1]
 * loaded on a live processor ([] when they all do). Only reads. */
export async function loadedDecksFaults(page, expected) {
	const decks = await page.evaluate((n) => {
		const q = window.musicDjToolsPerformance.query();
		return [...Array(n).keys()].map((i) => q.decks[i + 1] ?? null);
	}, expected.length);
	return decks.flatMap((dk, i) => {
		const r = loadedReason(dk, expected[i]);
		return r ? [`deck ${i + 1}: ${r}`] : [];
	});
}

/** Reasons decks 1..n are unhealthy right now ([] when all are healthy). The
 * decks' own state is the app's model of its output, so the harness also reads
 * what really reached the destination (audio-probe.mjs, which the page must
 * have installed): a muted master or a cut destination leaves every deck
 * "audible" while nothing is heard. The destination is the sum, so it also
 * hears each input of the mix: one deck cut while the others play leaves the
 * sum loud. Each call samples for about 400 ms over CDP, so it runs between
 * windows; inside one, monitorAudio watches passively. */
export async function unhealthyDecks(page, n) {
	const { decks, channels } = await page.evaluate((n) => {
		const q = window.musicDjToolsPerformance.query();
		return {
			decks: [...Array(n).keys()].map((i) => q.decks[i + 1] ?? null),
			channels: Object.keys(q.mixer?.channels ?? {}).length
		};
	}, n);
	const level = await page.evaluate(destinationLevelDbInPage, 200);
	const mix = channels >= n ? await page.evaluate(mixInputLevelsInPage, [200, channels]) : null;
	return decksReasons({ decks, channels, dest: level, mix }, n);
}

/** Why decks 1..n were not healthy in one reading ([] when they were): each
 * deck's queried state, the destination's level and each mix input's level.
 * Shared by the active check and the passive in-window watch, so both judge
 * a reading by the same rules. A watch bucket also carries silentMs, the
 * longest run of consecutive quiet reads that reached into it: a dropout
 * that resumes inside a span leaves the span's loudest read healthy, so a
 * run of SILENT_RUN_MS fails it on its own. */
export function decksReasons({ decks, channels, dest, mix, silentMs }, n) {
	const why = decks.flatMap((dk, i) => {
		const r = unhealthyReason(dk);
		return r ? [`deck ${i + 1}: ${r}`] : [];
	});
	const silent = silentReason(dest);
	// the channel sum has one input per mixer channel; without that count the
	// deck mix cannot be told from a fan-in that carries the whole mix
	const mixWhy =
		channels >= n
			? mixInputsReason(mix, n)
			: `the mixer reports ${channels} channels for ${n} decks, so each deck's input to the mix is unmeasured`;
	const dropout =
		silentMs >= SILENT_RUN_MS
			? `audio dropped out: consecutive quiet reads over >= ${SILENT_RUN_MS} ms (destination or a deck input of the mix below ${SILENT_DBFS} dBFS)`
			: null;
	return [...why, ...(silent ? [silent] : []), ...(mixWhy ? [mixWhy] : []), ...(dropout ? [dropout] : [])];
}

/** Waits up to timeoutMs for decks 1..n to settle into healthy playback; returns
 * the last reasons when they never do. A phase starts only after this passes. */
export async function awaitHealthyDecks(page, n, timeoutMs = 15_000) {
	const t0 = Date.now();
	for (;;) {
		const broken = await unhealthyDecks(page, n);
		if (!broken.length || Date.now() - t0 > timeoutMs) return broken;
		await new Promise((r) => setTimeout(r, 100));
	}
}

/** Reasons decks 1..n do not each reach the output ([] when every one does).
 * The destination probe hears the mix, so one deck cut or muted while three
 * play would pass it; this solos each deck in turn through the app's own
 * faders (the others at 0), reads the destination, and restores every fader.
 * It changes what is heard, so it runs only outside a measured window. */
export async function decksNotReachingOutput(page, n, { settleMs = 150, floorDb = SILENT_DBFS } = {}) {
	const faders = await page.evaluate(
		(n) => [...Array(n).keys()].map((i) => window.musicDjToolsPerformance.query().mixer.channels[i + 1].fader),
		n
	);
	const set = (deck, value) => page.evaluate((c) => window.musicDjToolsPerformance.dispatch(c), { type: 'fader', deck, value });
	const why = [];
	try {
		// Negative control through the same dispatcher: every fader down must
		// silence the output. A dispatcher that updates state but not the DSP
		// would leave every solo hearing the whole mix and pass a cut deck.
		for (let o = 1; o <= n; o++) await set(o, 0);
		await new Promise((r) => setTimeout(r, settleMs));
		if (silentReason(await page.evaluate(destinationLevelDbInPage, 200), floorDb) === null) {
			return ['every fader at 0 still reaches the output: fader dispatch does not reach the audio graph, so no deck can be soloed'];
		}
		for (let d = 1; d <= n; d++) {
			for (let o = 1; o <= n; o++) await set(o, o === d ? faders[o - 1] : 0);
			await new Promise((r) => setTimeout(r, settleMs));
			const r = silentReason(await page.evaluate(destinationLevelDbInPage, 200), floorDb);
			if (r) why.push(`deck ${d} solo: ${r}`);
		}
	} finally {
		for (let o = 1; o <= n; o++) await set(o, faders[o - 1]);
		await new Promise((r) => setTimeout(r, settleMs));
	}
	return why;
}

/** Watches decks 1..n for a measured window and resolves, once the watch is
 * running, to { stop() }, which ends it and resolves with one { t, broken }
 * sample per everyMs: every reason any span of that interval failed with
 * (decksReasons, judged per spanMs span, see startWatchInPage). A dropout that
 * recovers before the window ends would pass both endpoint checks while its
 * missing DSP work read as a saving, so the window is watched throughout, and
 * a dropout inside an interval fails it however loud the rest was. The watch
 * is passive: the measured renderer runs one cheap analyser read per readMs
 * and nothing else for it, so the harness's own probing is not charged to the
 * CPU and frames it is measuring. It only reads, so it never changes what is
 * heard. */
export async function monitorAudio(page, n, everyMs = 1000, readMs = 50, spanMs = 200) {
	const closed = () => typeof page.isClosed === 'function' && page.isClosed();
	const t0 = Date.now();
	await page.evaluate(startWatchInPage, [n, Math.min(spanMs, everyMs), readMs, SILENT_DBFS]);
	return {
		async stop() {
			let buckets;
			try {
				buckets = await page.evaluate(stopWatchInPage);
			} catch (e) {
				// the page closing under the watch loses its buckets: name it
				const why = closed() ? 'page closed during the audio watch' : `audio watch failed: ${String(e).slice(0, 200)}`;
				return [{ t: Date.now(), broken: [why] }];
			}
			if (!buckets) return [{ t: Date.now(), broken: ['the audio watch was not running when it was stopped'] }];
			// one sample per everyMs of the window, failing with every reason any of its spans failed with
			const samples = new Map();
			for (const b of buckets) {
				const why = b.error ? [`audio check failed: ${b.error}`] : b.reads ? decksReasons(b, n) : ['no analyser read in the span'];
				const key = Math.floor((b.t - t0) / everyMs);
				const s = samples.get(key) ?? { t: b.t, broken: [], quiet: 0 };
				s.t = b.t;
				s.quiet += b.quiet ?? 0;
				for (const w of why) if (!s.broken.includes(w)) s.broken.push(w);
				samples.set(key, s);
			}
			return [...samples.values()];
		}
	};
}

/** Why a window's audio cannot be trusted ([] when every check passed). Too
 * few checks is UNKNOWN, never a pass: an unwatched window proves nothing. */
export function windowAudioProblems(samples, minSamples) {
	if (samples.length < minSamples) return [`audio checked ${samples.length} times in the window, fewer than ${minSamples}`];
	const bad = samples.filter((s) => s.broken.length);
	return bad.length ? [`audio broke in ${bad.length} of ${samples.length} checks during the window: ${bad[0].broken.join('; ')}`] : [];
}
