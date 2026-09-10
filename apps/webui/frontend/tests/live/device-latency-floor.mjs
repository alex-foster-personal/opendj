/**
 * What each engine reports for its own output latency, and WHEN.
 *
 * This is the evidence behind the refusal rule in
 * `src/lib/player/transport/press-audible.ts`: a device floor of exactly
 * zero is the ABSENCE of a measurement, not a measurement of zero. That
 * rule is load-bearing (it decides whether a press-to-audible row may claim
 * `input_to_output_ms`), so the reading behind it lives here as a command
 * anyone can re-run rather than as a number somebody once pasted into a
 * comment.
 *
 * Run:  node tests/live/device-latency-floor.mjs
 *       (from apps/webui/frontend, so @playwright/test resolves)
 *
 * Reads on this Mac, Wed 9 Sep 2026:
 *
 *   WebKit 26.5          fresh  suspended  base 2.902ms  output  0.000ms
 *                        running running   base 2.902ms  output 15.964ms
 *   Chromium 149.0.7827  fresh  running    base 5.805ms  output  0.000ms
 *                        running running   base 5.805ms  output 32.000ms
 *
 * Note the Chromium row: the context reports `running` while
 * `outputLatency` is still 0. So the context STATE does not tell you
 * whether the floor is real, which is why the shipped guard tests the
 * value and not the state.
 *
 * Regression lines:
 *  - if either engine's fresh outputLatency stops reading 0 then the
 *    refusal rule is guarding a case that no longer occurs, and the comment
 *    that justifies it is stale
 *  - if a running outputLatency reads 0 then the rule is discarding a real
 *    reading and press-to-audible rows are refusing a floor they could have
 *  - if this script reports a version far from the ones above then the
 *    numbers are that engine's, not the ones the rule was written against
 */
import { chromium, webkit } from '@playwright/test';

/** One context, measured before and after it has actually rendered audio. */
const PROBE = async () => {
	const ctx = new AudioContext();
	const fresh = { state: ctx.state, base: ctx.baseLatency, out: ctx.outputLatency };
	const osc = ctx.createOscillator();
	osc.connect(ctx.destination);
	osc.start();
	await ctx.resume();
	// Long enough that the engine has filled at least one output quantum;
	// the property stays 0 until it has.
	await new Promise((resolve) => setTimeout(resolve, 600));
	const running = { state: ctx.state, base: ctx.baseLatency, out: ctx.outputLatency };
	await ctx.close();
	return { fresh, running };
};

const ms = (seconds) => (typeof seconds === 'number' ? `${(seconds * 1000).toFixed(3)}ms` : String(seconds));

for (const [label, type] of [
	['WebKit', webkit],
	['Chromium', chromium]
]) {
	const browser = await type.launch({
		// Chromium will not start a context without a gesture otherwise, and
		// this page has no operator to make one.
		args: type === chromium ? ['--autoplay-policy=no-user-gesture-required'] : []
	});
	const page = await browser.newPage();
	await page.goto('about:blank');
	const reading = await page.evaluate(PROBE);
	console.log(`${label.padEnd(9)} version=${browser.version()}`);
	for (const phase of ['fresh', 'running']) {
		const row = reading[phase];
		console.log(
			`  ${phase.padEnd(8)} state=${row.state.padEnd(9)} ` +
				`baseLatency=${ms(row.base)} outputLatency=${ms(row.out)}`
		);
	}
	await browser.close();
}
