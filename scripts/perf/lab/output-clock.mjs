// perf-lab: reading an AudioContext's output presentation clock.
//
// getOutputTimestamp() returns the context time the output is presenting now
// (contextTime) and, when Chromium has one, where that falls on the page's
// performance clock (performanceTime). As AGENTS.md states for transport
// truth, the context time is what says audio was presented: a stamp counts
// when its contextTime is finite, positive and not behind the last one counted,
// whatever its performanceTime; {contextTime: 0} is device warm-up, not output.
// performanceTime is only a correlation: when it is finite, positive and not
// regressing, it maps a context time to a page time exactly; otherwise the
// driver times the presentation by observing the context clock reach it.
// The driver injects these functions' source into the page.

/** True when ts says output is being presented: contextTime finite, positive
 * and not behind lastContextTime (the last stamp counted on this context). */
export function presentedStamp(ts, lastContextTime) {
	return Boolean(ts) && Number.isFinite(ts.contextTime) && ts.contextTime > 0 && ts.contextTime >= lastContextTime;
}

/** True when ts's performanceTime can map context time to page time: finite,
 * positive and not behind lastPerformanceTime. */
export function correlatedStamp(ts, lastPerformanceTime) {
	return Boolean(ts) && Number.isFinite(ts.performanceTime) && ts.performanceTime > 0 && ts.performanceTime >= lastPerformanceTime;
}

/** read()'s value, stamped with the moment and context time AFTER it was
 * read. The analyser can observe a render quantum that advanced between a
 * clock read and the analyser read; stamping afterwards keeps the context
 * time at or past the quantum the value came from, so presentedAt dates the
 * effect late by at most one quantum, never early. Injected into the page by
 * driver.mjs, so it uses nothing from this module's scope. */
export function readThenStamp(read, contextClock) {
	const value = read();
	return { value, at: performance.now(), ctxTime: contextClock() };
}

/** When a context time just rendered reached the listener: {at, method} on
 * the page clock, or null if the output never presents it within limitMs.
 * readStamp() returns the presented stamp ({contextTime, performanceTime},
 * performanceTime null when it does not correlate) or null. Presentation is
 * accepted only once the presented context clock has reached ctxTime: a
 * correlation alone would extrapolate a time for audio a stalled or
 * suspended output never played. The time is then the exact mapping of the
 * stamp that crossed it, when that stamp correlates, or else the moment the
 * clock was first seen past it (late by up to one poll, never early). An
 * earlier poll's correlation is never reused: the clock may have stalled or
 * been re-anchored since, so extrapolating it could date the presentation
 * before it happened, even before the command. Injected into the page by
 * driver.mjs, so it uses nothing from this module's scope. */
export async function presentedAt(readStamp, ctxTime, sleep, limitMs = 1000) {
	const tStart = performance.now();
	while (performance.now() - tStart < limitMs) {
		const ts = readStamp();
		if (ts !== null && ts.contextTime >= ctxTime) {
			return ts.performanceTime !== null
				? { at: ts.performanceTime + (ctxTime - ts.contextTime) * 1000, method: 'correlated' }
				: { at: performance.now(), method: 'observed' };
		}
		await sleep(2);
	}
	return null;
}
