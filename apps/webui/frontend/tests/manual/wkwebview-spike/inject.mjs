/**
 * Bundle entry for the DESK-005 spike harness.
 *
 * Bundled to one classic script so it can be pasted into the Web Inspector
 * console of a WKWebView that does not serve this directory:
 *
 *   cd apps/webui/frontend
 *   pnpm exec esbuild tests/manual/wkwebview-spike/inject.mjs \
 *     --bundle --format=iife --outfile=/tmp/wkwebview-spike-harness.js
 *   pbcopy < /tmp/wkwebview-spike-harness.js
 *
 * Paste into the console of the window that already has /performance loaded,
 * then follow docs/quality/desk-005-wkwebview-spike-runbook.md.
 *
 * Installation throws when the host cannot support the spike (no performance
 * IPC, wrong IPC version, no document), so a failed injection can never be
 * mistaken for a clean run.
 */

import { installSpikeHarness } from './harness.mjs';

const options = globalThis.__WKWEBVIEW_SPIKE_OPTIONS__ ?? {};
const harness = installSpikeHarness(globalThis, options);

globalThis.console.log(
	'DESK-005 harness installed as window.wkwebviewSpike. ' +
		'Next: wkwebviewSpike.begin({...host metadata...}), wkwebviewSpike.startSampling(), ' +
		'await wkwebviewSpike.runAutomatable(), then wkwebviewSpike.dump().'
);
globalThis.console.log(harness.capabilities());
