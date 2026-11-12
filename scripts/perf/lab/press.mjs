// One latency press, driven through the page's REAL control (LATENCY-01).
//
// A press is a real pointer gesture on the element the user would touch (the
// play button, the channel fader, the low EQ knob), sent by Playwright's
// mouse, and every time is measured from that input event's own timestamp
// (event.timeStamp, the page clock). The app's handler, whatever it does
// before it reaches the shared dispatcher, is therefore inside every number;
// calling the browser IPC directly would skip it, and a regression there would
// still read as a fast press. A control with no reachable element is never
// replaced by a dispatch: the row carries press_error naming it, and
// analyze.py reports that action UNKNOWN.
//
// Two modes, one per latency pass (driver.mjs):
//   budget       the untraced pass: the detectors, the low-band series and the
//                presentation mapping run, and their rows are LATENCY-01's.
//   attribution  the traced pass: analyze.py attributes the main-thread work
//                inside each perflab:latency:<action> measure to the press, so
//                that window holds no detector or series. It runs from the
//                input event to the painted frame that shows the control's new
//                state, and the only harness work inside it, the checks that
//                find that frame, runs inside perflab:probe measures, which
//                analyze.py moves out of the app's attribution (harness-probe).

import { paintedFrame, renderedControl } from './frame-clock.mjs';
import { correlatedStamp, presentedAt, presentedStamp, readThenStamp } from './output-clock.mjs';
import { silentBefore } from './audio-probe.mjs';

/** The helpers the in-page press reads from window, as one init script. */
export const PRESS_INIT = `window.__perflabPresentedStamp = ${presentedStamp.toString()}; window.__perflabCorrelatedStamp = ${correlatedStamp.toString()}; window.__perflabPresentedAt = ${presentedAt.toString()}; window.__perflabReadThenStamp = ${readThenStamp.toString()}; window.__perflabPaintedFrame = ${paintedFrame.toString()}; window.__perflabRenderedControl = ${renderedControl.toString()}; window.__perflabSilentBefore = ${silentBefore.toString()};`;

/** The element each action is pressed on (the data-testid the components
 * render; frame-clock.mjs renderedControl reads the same ones), and the input
 * event the press is timed from. idle_control presses nothing. */
export function controlOf(action, deck) {
	if (action === 'play' || action === 'pause') return { testid: `play-deck-${deck}`, event: 'click' };
	if (action === 'fader_cut' || action === 'fader_open') return { testid: `channel-${deck}-fader`, event: 'pointerdown' };
	// the kill is a downward drag (Knob.svelte: 120 px per full throw); the
	// pointermove that carries it is the input, the pointerdown only grabs
	if (action === 'eq_low_kill') return { testid: `knob-${deck}:low`, event: 'pointermove' };
	// a double-click resets the knob to its unity resetValue (Knob.svelte)
	if (action === 'eq_low_restore') return { testid: `knob-${deck}:low`, event: 'dblclick' };
	return null;
}

// A knob drag needs this much room below the knob's center to reach 0 from
// unity: half a throw of Knob.svelte's 120 px, plus margin.
const KNOB_KILL_DRAG_PX = 100;

/** Where the gesture starts and how it is sent, or why the control cannot be
 * pressed. Read before the press is armed: scrolling and hovering are not the
 * press. */
async function aim(page, action, deck) {
	const c = controlOf(action, deck);
	if (c === null) return { control: null };
	const loc = page.locator(`[data-testid="${c.testid}"]`);
	const n = await loc.count();
	if (n !== 1) return { error: `control ${c.testid}: ${n} elements on the page` };
	await loc.scrollIntoViewIfNeeded({ timeout: 2000 }).catch(() => {});
	const box = await loc.boundingBox();
	if (!box || box.width < 1 || box.height < 1) return { error: `control ${c.testid}: not rendered (no box)` };
	if (await loc.isDisabled().catch(() => false)) return { error: `control ${c.testid}: disabled` };
	const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
	// a fader press lands at the end of the track it commands (VFader.svelte
	// clamps the thumb-centered position to 0..1)
	const at = action === 'fader_cut' ? [cx, box.y + box.height - 1] : action === 'fader_open' ? [cx, box.y + 1] : [cx, cy];
	const view = page.viewportSize();
	if (action === 'eq_low_kill' && view && cy + KNOB_KILL_DRAG_PX > view.height - 1) {
		return { error: `control ${c.testid}: no room below it to drag the knob to 0` };
	}
	// the element the pointer would really hit there must be the control
	const hits = await page.evaluate(({ id, x, y }) => {
		const el = document.querySelector(`[data-testid="${id}"]`);
		const top = document.elementFromPoint(x, y);
		return el !== null && top !== null && el.contains(top);
	}, { id: c.testid, x: at[0], y: at[1] });
	if (!hits) return { error: `control ${c.testid}: covered at (${Math.round(at[0])}, ${Math.round(at[1])})` };
	await page.mouse.move(at[0], at[1]);
	return { control: c, at };
}

/** The real input for action, at the point aim() chose. */
async function gesture(page, action, at) {
	const [x, y] = at;
	if (action === 'play' || action === 'pause') return page.mouse.click(x, y);
	if (action === 'fader_cut' || action === 'fader_open') {
		await page.mouse.down();
		return page.mouse.up();
	}
	if (action === 'eq_low_kill') {
		await page.mouse.down();
		await page.mouse.move(x, y + KNOB_KILL_DRAG_PX);
		return page.mouse.up();
	}
	if (action === 'eq_low_restore') return page.mouse.dblclick(x, y);
	throw new Error(`no gesture for ${action}`);
}

/** One press of action on deck: the row driver.mjs records. attribution
 * selects the traced pass's mode (see the header). */
export async function press(page, { action, deck, FADER_OPEN, EQ_UNITY, attribution = false }) {
	const aimed = await aim(page, action, deck);
	if (aimed.error) return unpressedRow(action, aimed.error);
	const token = `${action}:${Math.random()}`;
	const control = aimed.control;
	const row = page.evaluate(pressInPage, { action, deck, FADER_OPEN, EQ_UNITY, attribution, token, control });
	if (control) {
		// the press listens for its input before the input is sent
		const armed = await page
			.waitForFunction((t) => window.__perflabArmed === t, token, { timeout: 15_000 })
			.then(() => true, () => false);
		if (armed) await gesture(page, action, aimed.at);
	}
	return row;
}

/** The row of a press that never happened, every measurement null. */
function unpressedRow(action, why) {
	return {
		action, t0: null, press_error: why, input: null,
		reflected_ms: null, rendered_ms: null, next_frame_ms: null,
		detectors: [], detectors_usable: [], detectors_fired: [],
		baseline_peak: null, baseline_silent: null, low_ratio_series: null, output_stamp: null,
		destination_audible_ms: null, presented_ms: null, presented_method: null,
		base_latency_ms: null, output_latency_ms: null
	};
}

// Runs inside the page (page.evaluate): self-contained, reads only window.
async function pressInPage({ action, deck, FADER_OPEN, EQ_UNITY, attribution, token, control }) {
	const ipc = window.musicDjToolsPerformance;
	// the traced pass hears nothing: its window must hold no detector
	const probe = attribution ? undefined : window.__perflabAudio?.contexts?.[0];
	const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
	// harness work that has to run inside an attributed window is measured as
	// perflab:probe, which analyze.py moves out of the press's attribution
	const probeTick = (fn) => {
		const s = performance.now();
		try { return fn(); } finally { performance.measure('perflab:probe', { start: s, end: performance.now() }); }
	};
	const peak = () => {
		const buf = new Float32Array(probe.analyser.fftSize);
		probe.analyser.getFloatTimeDomainData(buf);
		let p = 0; for (const v of buf) p = Math.max(p, Math.abs(v));
		return p;
	};
	// energy below 150 Hz over energy at 1-4 kHz, in dB: the low shelf
	// cuts the first by ~26 dB at a kill and leaves the second alone,
	// so the ratio moves on the EQ and not on overall loudness
	const lowRatioDb = () => {
		const f = new Float32Array(probe.spectrum.frequencyBinCount);
		probe.spectrum.getFloatFrequencyData(f);
		const hz = probe.ctx.sampleRate / probe.spectrum.fftSize;
		let lo = 0, hi = 0;
		for (let i = 1; i < f.length; i++) {
			const e = 10 ** (f[i] / 10), fr = i * hz;
			if (fr < 150) lo += e; else if (fr >= 1000 && fr < 4000) hi += e;
		}
		return lo > 0 && hi > 0 ? 10 * Math.log10(lo / hi) : null;
	};
	// The detectors. Each poll returns 0 (not heard), 1 (heard, not yet
	// confirmed) or 2 (confirmed); the effect is timed from the first
	// poll of the confirming run. The falls need 3 polls in a row past
	// their threshold, so one quiet window between kicks is not a cut.
	const sustained = (below) => {
		let run = 0;
		return () => { run = below() ? run + 1 : 0; return run >= 3 ? 2 : run > 0 ? 1 : 0; };
	};
	const detectors = {
		sound: () => () => (peak() > 0.01 ? 2 : 0),
		level_drop: (b) => sustained(() => peak() < b.peak / 10),
	};
	// The output presentation clock (output-clock.mjs). outputStamp()
	// returns the current presented stamp, with performanceTime kept only
	// when it correlates; null while the output is not presenting.
	const outputStamp = () => {
		const ts = probe.ctx.getOutputTimestamp?.();
		if (!window.__perflabPresentedStamp(ts, probe.lastTsContextTime)) return null;
		probe.lastTsContextTime = ts.contextTime;
		const corr = window.__perflabCorrelatedStamp(ts, probe.lastTsPerformanceTime);
		if (corr) probe.lastTsPerformanceTime = ts.performanceTime;
		return { contextTime: ts.contextTime, performanceTime: corr ? ts.performanceTime : null };
	};
	// When a context time just rendered reaches the listener (output-clock.mjs
	// presentedAt): only once the presented clock reaches it.
	const presentedAt = (ctxTime) => window.__perflabPresentedAt(outputStamp, ctxTime, sleep);
	// The input event's page time, once it arrives; the detectors' budgets run
	// from it, and from the arming until then.
	let inputAt = null;
	const since = () => inputAt ?? tArm;
	const watch = async (fire, timeoutMs) => {
		let runStart = null;
		while (performance.now() - since() < timeoutMs) {
			// the analyser first, then the clocks (output-clock.mjs readThenStamp)
			const { value: v, at: now, ctxTime } = window.__perflabReadThenStamp(fire, () => probe.ctx.currentTime);
			if (v === 0) runStart = null;
			else runStart ??= { at: now, ctxTime };
			if (v === 2) return runStart;
			await sleep(1);
		}
		return null;
	};
	// The level-drop baseline: the median peak over the half second
	// before the press; null when the deck is too quiet to hear a fall.
	// The low-band ratio is RECORDED over the same half second and 1.5 s
	// after the press ([ms from press, context time, ratio dB]) and the
	// kill is found in it by analyze.py (low_kill_onset), where the
	// detector can be tested and tuned against idle_control offline: on
	// music it is too noisy to decide on line (live Wed 14 Oct 2026, a
	// median-minus-12-dB rule fired on 53 of 100 idle reps).
	let basePeak = null;
	const series = [];
	// One series sample. The page time is read LAST: analyze.py bounds
	// presentation by the first sample whose output clock passed the
	// effect, so its time must not precede that clock read (late by up
	// to one poll, never early).
	// The 5th element is that presented stamp's own correlated page time
	// (null when it does not correlate): analyze.py maps the onset through
	// the sample that first crossed it, never a later re-anchored stamp.
	const sample = () => {
		// the analyser before the context time; the page time after the output clock
		const { value: db, ctxTime } = window.__perflabReadThenStamp(lowRatioDb, () => probe.ctx.currentTime);
		const st = outputStamp();
		return [performance.now(), ctxTime, db, st?.contextTime ?? null, st?.performanceTime ?? null];
	};
	const recordSeries = probe && (action === 'idle_control' || action === 'eq_low_kill');
	if (probe && ['idle_control', 'fader_cut', 'eq_low_kill'].includes(action)) {
		const peaks = [];
		for (const t = performance.now(); performance.now() - t < 500; await sleep(5)) {
			peaks.push(peak());
			if (recordSeries) series.push(sample());
		}
		const pk = [...peaks].sort((a, b) => a - b)[peaks.length >> 1];
		basePeak = pk > 0.01 ? pk : null;
	}
	// after the frame showing the change was painted (frame-clock.mjs)
	const nextFrame = () => window.__perflabPaintedFrame();
	// one task boundary (MessageChannel, not clamped like setTimeout)
	const yieldTask = () => new Promise((r) => { const c = new MessageChannel(); c.port1.onmessage = () => r(); c.port2.postMessage(0); });
	// Resolves with the time of a task posted from the MutationObserver callback
	// that sees the write. That callback runs in the writing frame, after the
	// app's render callback, so the task runs after this frame's paint and before
	// the next frame's callbacks (a rAF posted here would run in the next frame,
	// after its extra work). A rAF timestamp is the frame's begin time, before
	// the paint, so it is not used as the endpoint. dispose() disconnects the
	// observer on every path, so an unused one never outlives the press.
	const renderedAfterPaint = (action, deck, shown) => {
		let obs = null;
		const dispose = () => { obs?.disconnect(); obs = null; };
		const promise = new Promise((resolve) => {
			const finish = () => {
				dispose();
				const c = new MessageChannel();
				c.port1.onmessage = () => { c.port1.close(); resolve(performance.now()); };
				c.port2.postMessage(0);
			};
			obs = new MutationObserver(() => { if (window.__perflabRenderedControl(action, deck, shown)) finish(); });
			obs.observe(document.body, { attributes: true, subtree: true });
			if (window.__perflabRenderedControl(action, deck, shown)) finish();
		});
		return { promise, dispose };
	};
	// The state the press must bring about: what the dispatcher's query
	// reflects, and the value the page's control must show.
	let commanded = true, reflected = () => true, armed = [], shown = null;
	if (action === 'play') { reflected = (q) => q.decks[deck].playing === true && q.decks[deck].transport_pending === false; armed = ['sound']; }
	else if (action === 'pause') { reflected = (q) => q.decks[deck].playing === false && q.decks[deck].transport_pending === false; }
	else if (action === 'idle_control') { commanded = false; armed = ['level_drop']; }
	else if (action === 'fader_cut') { shown = 0; reflected = (q) => q.mixer.channels[deck].fader === 0; armed = ['level_drop']; }
	else if (action === 'fader_open') { shown = FADER_OPEN; reflected = (q) => Math.abs(q.mixer.channels[deck].fader - FADER_OPEN) < 1e-6; armed = ['sound']; }
	else if (action === 'eq_low_kill') { shown = 0; reflected = (q) => q.mixer.channels[deck].eq_low === 0; }
	else { shown = EQ_UNITY; reflected = (q) => Math.abs(q.mixer.channels[deck].eq_low - EQ_UNITY) < 1e-6; }
	// An onset is a silent-to-audible transition: over output that is
	// already sounding (a pause or cut that never reached it) the sound
	// detector would fire at once and time nothing, so it arms only on a
	// baseline silent throughout, and the row is otherwise unmeasured.
	let baselineSilent = null;
	if (probe && armed.includes('sound')) {
		const peaks = [];
		for (const t = performance.now(); performance.now() - t < 150; await sleep(5)) peaks.push(peak());
		baselineSilent = window.__perflabSilentBefore(peaks);
	}
	// a fall detector with no usable baseline cannot hear anything
	const usable = armed.filter((d) => probe && (d === 'sound' ? baselineSilent === true : basePeak !== null));
	// Listen for the real input on the control. The listener runs in the
	// capture phase, before the app's own handler, and only notes the
	// event's timestamp. A knob drag's input is the pointermove that carries
	// the button, never a hover.
	const el = control ? document.querySelector(`[data-testid="${control.testid}"]`) : null;
	let onInput = () => {};
	const gotInput = new Promise((r) => { onInput = r; });
	const listener = (e) => {
		if (inputAt !== null || el === null || !(e.target instanceof Node) || !el.contains(e.target)) return;
		if (control.event === 'pointermove' && (e.buttons & 1) === 0) return;
		inputAt = e.timeStamp;
		onInput();
	};
	if (control) window.addEventListener(control.event, listener, true);
	const tArm = performance.now();
	// Watch the destination from the arming, alongside the press: a fast press
	// can turn audible before the app's state reflects it. The analyser shows
	// the graph RENDERED the effect, not that the output presented it, so the
	// detection's context time is mapped through presentedAt(). That point is
	// the end of the analyser window, so it can be late by up to one window
	// (512 or 2048 samples), never early.
	const heard = usable.length
		? Promise.all(usable.map(async (d) => ({ d, hit: await watch(detectors[d]({ peak: basePeak }), action === 'idle_control' ? 1500 : 2000) })))
		: Promise.resolve([]);
	const recorded = recordSeries
		? (async () => {
				while (performance.now() - since() < 1500) { series.push(sample()); await sleep(5); }
			})()
		: Promise.resolve();
	let pressError = null;
	if (control) {
		window.__perflabArmed = token;
		await Promise.race([gotInput, sleep(10_000)]);
		window.removeEventListener(control.event, listener, true);
		window.__perflabArmed = null;
		if (inputAt === null) pressError = `control ${control.testid}: no ${control.event} reached it`;
	}
	// idle_control presses nothing: its time zero is the arming
	const t0 = inputAt ?? tArm;
	inputAt = t0;
	performance.mark(`perflab:press:${action}`, { startTime: t0 });
	// Attribution polls between tasks, never inside requestAnimationFrame: a
	// poll inside a frame callback that then awaited the next frame would end
	// the window one frame late. The app's render writes the control in a frame
	// callback, so the first task that reads it runs after that frame's paint,
	// and that task is the post-paint moment the window ends at. The budget
	// pass polls every millisecond, untraced.
	const pause = attribution ? yieldTask : () => sleep(1);
	// attribution only: armed before the input, so the change is caught the
	// moment the app's frame writes it (a mutation callback, in that frame)
	// only a commanded press has a write to wait for: an idle row arms nothing
	const rendered = attribution && commanded ? renderedAfterPaint(action, deck, shown) : null;
	try {
	const check = attribution ? probeTick : (fn) => fn();
	let tReflected = null;
	for (let i = 0; commanded && pressError === null && i < 2000 && tReflected === null; i++) {
		if (check(() => reflected(ipc.query()))) tReflected = performance.now(); else await pause();
	}
	// the frame that matters paints the CONTROL's new state, so wait until
	// the control renders it; a control that never does has no frame
	let tRendered = null;
	if (attribution) {
		// the observer's post-paint task (renderedAfterPaint), never a poll that
		// could land after the next frame's callbacks
		if (commanded && tReflected !== null) tRendered = await Promise.race([rendered.promise, sleep(5000).then(() => null)]);
	} else {
		for (let i = 0; commanded && tReflected !== null && i < 2000 && tRendered === null; i++) {
			if (window.__perflabRenderedControl(action, deck, shown)) tRendered = performance.now(); else await sleep(1);
		}
	}
	const tFrame = attribution ? tRendered : await nextFrame();
	if (commanded && pressError === null && tReflected === null) pressError = `the app never reflected ${action} after its input`;
	// The traced pass's window ends at the painted frame showing the effect:
	// nothing the harness does after it (or the detectors, which do not run
	// there) is the press's work. A press with no rendered frame has no window.
	if (attribution && tRendered !== null) performance.measure(`perflab:latency:${action}`, { start: t0, end: tFrame });
	const hits = (await heard).filter((h) => h.hit);
	const first = hits.sort((a, b) => a.hit.at - b.hit.at)[0] ?? null;
	const presented = first ? await presentedAt(first.hit.ctxTime) : null;
	await recorded;
	// a correlated stamp maps any recorded context time to its presentation;
	// without one, analyze.py uses each sample's presented context time
	let seriesStamp = null;
	for (const t = performance.now(); recordSeries && seriesStamp?.performanceTime == null && performance.now() - t < 500; await sleep(5)) seriesStamp = outputStamp();
	let outputLatencyMs = null, baseLatencyMs = null;
	if (probe) { outputLatencyMs = (probe.ctx.outputLatency ?? 0) * 1000; baseLatencyMs = (probe.ctx.baseLatency ?? 0) * 1000; }
	return {
		action, t0, press_error: pressError,
		input: control ? { control: control.testid, event: control.event } : null,
		reflected_ms: commanded && tReflected !== null ? tReflected - t0 : null,
		rendered_ms: tRendered !== null ? tRendered - t0 : null,
		// the frame that shows the change; none when the control never rendered it
		next_frame_ms: tRendered !== null ? tFrame - t0 : null,
		detectors: armed, detectors_usable: usable, detectors_fired: hits.map((h) => h.d),
		baseline_peak: basePeak, baseline_silent: baselineSilent,
		low_ratio_series: recordSeries ? series.map(([t, c, r, pc, pp]) => [t - t0, c, r, pc, pp == null ? null : pp - t0]) : null,
		output_stamp: seriesStamp === null ? null : { ...seriesStamp, performanceTime: seriesStamp.performanceTime === null ? null : seriesStamp.performanceTime - t0 },
		destination_audible_ms: first ? first.hit.at - t0 : null,
		presented_ms: presented === null ? null : presented.at - t0,
		presented_method: presented?.method ?? null,
		base_latency_ms: baseLatencyMs, output_latency_ms: outputLatencyMs
	};
	} finally {
		// the observer goes on every exit path, including a throw above
		rendered?.dispose();
	}
}
