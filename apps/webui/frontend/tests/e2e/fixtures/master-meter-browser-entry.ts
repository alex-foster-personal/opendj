/**
 * Browser entry exposing the REAL master-meter module to the meter-artifact
 * Playwright suite.
 *
 * WHY THIS EXISTS (Sol thread 3967976238, P1 BLOCKING). The first browser test
 * for the master path hand-built an oscillator -> GainNode -> worklet -> sink
 * graph that merely LOOKED like what the production code does. It therefore
 * proved only that the worklet can observe some GainNode: it would have stayed
 * green with `createMasterMeterSource` deleted, or with `attachMeterTaps`
 * never connecting its source. That is precisely the "test the shape, not the
 * code" failure AGENTS.md L125-L133 forbids.
 *
 * So this entry imports the production module itself. The suite bundles it
 * with esbuild (the same tool `tests/unit/load-typescript.mjs` already uses),
 * pointed at the hashed worklet asset the real build emitted, and loads it into
 * the built app's page. Every function the test calls is then the shipped one:
 * delete the `source.connect(node)` in `attachMeterTaps` and the assertions go
 * red, which is the whole point.
 *
 * One entry, not several imports, for the reason
 * `tests/unit/fixtures/meter-unavailable-entry.ts` states: meter-tap.ts keeps
 * module-level state (the tap, the arming context, the unavailable flag), so
 * two bundles would give two unrelated copies of it and a test could pass
 * against a module the app never uses.
 */
import {
	SILENT_METER_READING,
	UNAVAILABLE_METER_READING,
	attachMeterTaps,
	createMasterMeterSource,
	markMetersUnavailable,
	masterMeterNode,
	masterMeterReading,
	meterClockMs,
	metersUnavailable,
	onMetersUnavailableChange,
	releaseMasterMeterTap,
	teardownMeterTaps
} from '$lib/rb/meter-tap';

const harness = {
	SILENT_METER_READING,
	UNAVAILABLE_METER_READING,
	attachMeterTaps,
	createMasterMeterSource,
	markMetersUnavailable,
	masterMeterNode,
	masterMeterReading,
	meterClockMs,
	metersUnavailable,
	onMetersUnavailableChange,
	releaseMasterMeterTap,
	teardownMeterTaps
} as const;

export type MasterMeterHarness = typeof harness;

declare global {
	interface Window {
		__masterMeterHarness?: MasterMeterHarness;
	}
}

window.__masterMeterHarness = harness;
