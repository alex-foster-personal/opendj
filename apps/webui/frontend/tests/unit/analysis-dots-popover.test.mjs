import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const componentsDir = path.join(__dirname, '../../src/lib/components/rb/browser');

/** Strip line + block comments before scanning for real code references, so
 * doc-comment prose ("AnalysisDotsPopover.svelte wraps this with...") never
 * false-positives as an actual import/fetch. Same convention as
 * pairings-inert.test.mjs. */
function stripComments(src) {
	return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
}

test('AnalysisDots.svelte stays pure: no jobProgress facade, no fetch', () => {
	const src = stripComments(
		readFileSync(path.join(componentsDir, 'AnalysisDots.svelte'), 'utf8')
	);
	// AnalysisDots legitimately imports constants/types (ANALYSIS_DOT_SLOTS,
	// ANALYSIS_COLORS, etc) from job-progress.svelte - the purity line is
	// about the live `jobProgress` STORE and the ingest client, not the
	// module path itself.
	assert.ok(!/\bjobProgress\b/.test(src), 'must not reach for the live jobProgress store');
	assert.ok(!src.includes('api-ingest'), 'must not import the ingest API');
	assert.ok(!/\bfetch\(/.test(src), 'must not call fetch directly');
});

test('AnalysisDots.svelte supports suppressDotTitles to defer to the popover', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDots.svelte'), 'utf8');
	assert.ok(src.includes('suppressDotTitles'), 'prop must exist');
	assert.ok(
		src.includes('if (suppressDotTitles) return undefined;'),
		'dotTitle must early-return when suppressed'
	);
});

test('AnalysisDotsPopover.svelte reaches for the live jobProgress facade and api-ingest', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDotsPopover.svelte'), 'utf8');
	assert.ok(src.includes("from '$lib/rb/job-progress.svelte'"), 'must import jobProgress facade');
	assert.ok(src.includes("from '$lib/rb/api-ingest'"), 'must import the ingest API');
	assert.ok(src.includes('startIngestRefresh'), 'must call the real ingest-refresh client');
});

test('AnalysisDotsPopover.svelte forwards suppressDotTitles to the inner AnalysisDots', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDotsPopover.svelte'), 'utf8');
	assert.ok(
		src.includes('suppressDotTitles={true}'),
		'must suppress the inner native per-dot titles in favour of its own popover'
	);
});

test('AnalysisDotsPopover.svelte maps ingest steps honestly (vocals/stems/else analysis)', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDotsPopover.svelte'), 'utf8');
	assert.ok(src.includes("if (kind === 'vocals') return 'vocals';"));
	assert.ok(src.includes("if (kind === 'stems') return 'stems';"));
	assert.ok(src.includes("return 'analysis';"));
});

test('AnalysisDotsPopover.svelte order() guards against double-fire and re-entrancy', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDotsPopover.svelte'), 'utf8');
	assert.ok(
		src.includes('if (stableId === null || ordering !== null) return;'),
		'must refuse to order with no track or a request already in flight'
	);
});

// REQ: LIBUX-11
test('AnalysisDotsPopover.svelte orders through the track-scoped HTTP client', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDotsPopover.svelte'), 'utf8');
	assert.ok(src.includes('orderTrackAnalysis'), 'must use the shared track order client');
	assert.ok(src.includes('getTrackAnalysisOrders'), 'must read agent-created track orders');
});

test('AnalysisDotsPopover.svelte derives coverage states through the shared job helper', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDotsPopover.svelte'), 'utf8');
	assert.ok(src.includes('analysisStatus('), 'must show the same queued/running state as job progress');
});

test('AnalysisDotsPopover.svelte states unavailability when there is no track', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDotsPopover.svelte'), 'utf8');
	assert.ok(src.includes('No track for this row - ordering is unavailable.'));
});

test('TrackTable.svelte wires both analysis cells through AnalysisDotsPopover with stableId', () => {
	const src = readFileSync(
		path.join(componentsDir, 'TrackTable.svelte'),
		'utf8'
	);
	assert.ok(src.includes("import AnalysisDotsPopover from './AnalysisDotsPopover.svelte';"));
	assert.ok(!src.includes("import AnalysisDots from './AnalysisDots.svelte';"));
	assert.ok(src.includes('<AnalysisDotsPopover badge={_badgeFor(row)} stableId={row.stable_id} />'));
	assert.ok(
		src.includes('<AnalysisDotsPopover issues={_issuesFor(row)} mode="issues" stableId={row.stable_id} />')
	);
});

// The "response confirms the step actually ran before marking it queued"
// behaviour used to be asserted here by src.indexOf ORDERING of two string
// literals - which cannot catch a real regression (Sol review on #1291,
// PRRT_kwDOSEvNd86fkmIn: a refactor that preserves the two strings but
// reorders or short-circuits the logic between them would still pass).
// That decision is now extracted to src/lib/rb/analysis-order.ts and
// exercised for real in tests/unit/analysis-order.test.mjs (stubs only the
// transport boundary, asserts upsertJob is never called on an excluded
// response). This file keeps only the source check that the component
// actually DELEGATES to the extraction rather than re-inlining its own copy
// of the decision.
test('AnalysisDotsPopover.svelte delegates the queued-confirmation decision to analysis-order.ts', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDotsPopover.svelte'), 'utf8');
	assert.ok(
		src.includes("import { runAnalysisOrder } from '$lib/rb/analysis-order';"),
		'must import the extracted, unit-tested decision rather than re-inlining it'
	);
	assert.ok(
		!src.includes('if (!status.steps.includes(stepId))'),
		'the inclusion check must live in analysis-order.ts, not be duplicated here'
	);
});

test('AnalysisDotsPopover.svelte never fires order() on an unconfirmed-enabled step', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDotsPopover.svelte'), 'utf8');
	assert.ok(
		src.includes("if (_stepEnabled(kind) !== true) return;"),
		'order() must refuse to run unless the step is confirmed enabled, never fire on null/false'
	);
});

test('AnalysisDotsPopover.svelte disables a row whose mapped step is confirmed disabled, with an honest reason', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDotsPopover.svelte'), 'utf8');
	assert.ok(src.includes('disabled in ingest config - enable it to queue'));
	assert.ok(src.includes("if (enabled === false) {"));
});

test('AnalysisDotsPopover.svelte does not offer to queue while the ingest config is still unknown', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDotsPopover.svelte'), 'utf8');
	assert.ok(src.includes("if (enabled === null) {"));
	assert.ok(src.includes('checking ingest config'));
});

test('AnalysisDotsPopover.svelte reports (not hides) a failed ingest-config check', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDotsPopover.svelte'), 'utf8');
	assert.ok(src.includes("ingestConfigStatus === 'error'"));
	assert.ok(src.includes('could not verify ingest config - not offering to queue'));
});

test('AnalysisDotsPopover.svelte is keyboard/focus reachable, not hover-only', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDotsPopover.svelte'), 'utf8');
	assert.ok(src.includes('tabindex="0"'), 'the wrapper must be in the tab order');
	assert.ok(src.includes('onfocusin={onFocusIn}'), 'must open on focus, not only on mouseenter');
	assert.ok(src.includes('onfocusout={onFocusOut}'));
	assert.ok(src.includes("e.key === 'Escape'"), 'must support closing via keyboard');
	// Strip comments first: the doc comment discusses the OLD role="tooltip"
	// by name as history, which must not false-positive against the markup.
	const code = stripComments(src);
	assert.ok(
		!code.includes('role="tooltip"'),
		'a tooltip role must never contain focusable content (the row buttons) - ARIA violation the popover had before this fix'
	);
});

test('AnalysisDotsPopover.svelte caches the ingest config across instances rather than refetching per row', () => {
	const src = readFileSync(path.join(componentsDir, 'AnalysisDotsPopover.svelte'), 'utf8');
	assert.ok(src.includes('<script module lang="ts">'), 'the shared cache must live at module scope, not per-instance');
	assert.ok(src.includes('_sharedIngestConfig'));
	assert.ok(src.includes('CONFIG_TTL_MS'));
});
