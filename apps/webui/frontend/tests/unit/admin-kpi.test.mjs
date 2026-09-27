import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// The /admin KPI route had NO frontend test file at all, while carrying six
// hand-dictated behavioural contracts. This file closes that hole.
//
//   H4 - a hand-typed or config-constant KPI must never render as a measurement
//   M1 - unmeasured is not zero
//   M3 - the whole stat card is the hover target
//   M4 - durations render in human units; an unknown direction is a hard error
//   M6 - the panel reads the ledger through the daemon and fails loudly
//
// (M5, run-note pagination, stays in the component's own $state and is covered
// structurally at the end rather than pretended to be unit-tested.)
//
// Regression lines:
// - if a configured-not-measured reading loses its badge then a config constant
//   reads as observed concurrency and a near-serial farm looks healthy
// - if a hand-entered reading loses its badge then typed and derived numbers are
//   pixel identical
// - if `derived` GAINS a badge then badge noise trains the eye to ignore badges
// - if a null snapshot enters the plotted points then a missing measurement
//   reads as a collapse to zero
// - if a real 0 is filtered out then a genuine zero is suppressed as missing
// - if judge() gains a default branch then a regression reads as an improvement
// - if a duration renders as raw seconds then the panel is unreadable again
// - if the ledger fetch stops validating then a junk tile renders undefined
// - if the tip action stops binding focus then the explainer is mouse-only

const ADMIN = fileURLToPath(new URL('../../src/routes/admin', import.meta.url));

let provenance;
let card;
let spark;
let format;
let kpiApi;

before(async () => {
	provenance = await loadTypeScriptModule('src/routes/admin/kpi-provenance.ts');
	card = await loadTypeScriptModule('src/routes/admin/kpi-card.ts');
	spark = await loadTypeScriptModule('src/routes/admin/spark-geometry.ts');
	format = await loadTypeScriptModule('src/routes/admin/format.ts');
	kpiApi = await loadTypeScriptModule('src/routes/admin/kpi-api.ts');
});

const GPU_CAP = {
	label: 'max parallel GPUs',
	unit: 'gpus',
	direction: 'higher_better',
	title: 'Concurrency ceiling for the farm run.'
};

function snapshot(label, values, provenanceMap = {}) {
	return { ts: `2026-08-0${label}T00:00:00Z`, label: `run ${label}`, values, provenance: provenanceMap, notes: null };
}

// ============================================================ H4: provenance

test('a configured-not-measured reading is badged config, not shown as observed', () => {
	const model = card.buildKpiCard('max_parallel_gpus', GPU_CAP, [
		snapshot('1', { max_parallel_gpus: 8 }, { max_parallel_gpus: 'configured-not-measured' })
	]);

	assert.equal(
		model.badge,
		'config',
		'a config constant with no badge reads as observed concurrency'
	);
	const explainer = model.tip.body.join(' ');
	assert.match(explainer, /CONFIG CONSTANT/, 'the explainer must name it as a config constant');
	assert.match(
		explainer,
		/ceiling the run was allowed/,
		'the explainer must say it is the ceiling allowed, not what the run reached'
	);
	assert.ok(
		!/DERIVED/.test(explainer),
		'a config constant must never claim it was derived from telemetry'
	);
});

test('a hand-entered reading is badged typed, verifiable or not', () => {
	for (const origin of ['hand', 'hand-unverifiable']) {
		const model = card.buildKpiCard('human_quality_1to10', GPU_CAP, [
			snapshot('1', { human_quality_1to10: 6 }, { human_quality_1to10: origin })
		]);
		assert.equal(model.badge, 'typed', `${origin} must badge as typed`);
		assert.match(
			model.tip.body.join(' '),
			/HAND-ENTERED/,
			`${origin} must say in prose that a human typed it`
		);
	}
	assert.match(
		provenance.ORIGIN_TEXT['hand-unverifiable'],
		/No telemetry survives/,
		'hand-unverifiable must say WHY nothing can check it, or it is the same as hand'
	);
});

test('a derived reading carries no badge at all', () => {
	const model = card.buildKpiCard('per_track_wall_s', GPU_CAP, [
		snapshot('1', { per_track_wall_s: 12 }, { per_track_wall_s: 'derived' })
	]);
	assert.equal(model.badge, '', 'badging every card trains the eye to ignore badges');
	assert.match(model.tip.body.join(' '), /DERIVED/, 'the origin is still explained on hover');
});

test('typed, config and derived are three visually distinguishable states', () => {
	const badges = new Set(
		['derived', 'hand', 'hand-unverifiable', 'configured-not-measured'].map((origin) =>
			provenance.originBadge(origin)
		)
	);
	// '' (derived), 'typed' (both hand kinds), 'config'
	assert.deepEqual([...badges].sort(), ['', 'config', 'typed']);
	assert.ok(
		provenance.isUnmeasuredOrigin('configured-not-measured'),
		'a config constant is not a measurement'
	);
	assert.ok(provenance.isUnmeasuredOrigin('hand'), 'a typed number is not a measurement');
	assert.ok(!provenance.isUnmeasuredOrigin('derived'), 'a derived number IS a measurement');
});

test('a snapshot written before provenance existed claims no origin', () => {
	const model = card.buildKpiCard('per_track_wall_s', GPU_CAP, [
		snapshot('1', { per_track_wall_s: 12 }, {})
	]);
	assert.equal(model.badge, '', 'an absent origin must not badge');
	const explainer = model.tip.body.join(' ');
	assert.ok(
		!/Origin:/.test(explainer),
		'an unknown origin must not be narrated as if it were known'
	);
});

test('every badged origin has an explainer, and every explainer an origin string', () => {
	for (const origin of Object.keys(provenance.BADGE_TEXT)) {
		assert.equal(
			typeof provenance.ORIGIN_TEXT[origin],
			'string',
			`${origin} shows a badge with nothing explaining it on hover`
		);
	}
	for (const [origin, text] of Object.entries(provenance.ORIGIN_TEXT)) {
		assert.match(text, /^Origin: /, `${origin}'s explainer does not name itself as an origin`);
	}
});

// ============================================================= M1: gaps

test('a null reading is a dashed gap tick, never a plotted point', () => {
	const geometry = spark.sparkGeometry('m', [
		snapshot('1', { m: 10 }),
		snapshot('2', { m: null }),
		snapshot('3', { m: 12 })
	]);

	assert.deepEqual(
		geometry.present.map((p) => p.index),
		[0, 2],
		'the null snapshot was plotted - a missing measurement now reads as a value'
	);
	assert.deepEqual(geometry.missing, [1], 'the gap has no tick to render');
	assert.equal(geometry.bounds.lo, 10, 'the null dragged the axis floor down');
	assert.equal(geometry.midlineY, 22, 'gap ticks must sit on the midline, not at the floor');
});

test('a segment spanning an unmeasured run is dashed', () => {
	const geometry = spark.sparkGeometry('m', [
		snapshot('1', { m: 10 }),
		snapshot('2', { m: null }),
		snapshot('3', { m: 12 }),
		snapshot('4', { m: 14 })
	]);

	assert.equal(geometry.segments.length, 2);
	assert.equal(geometry.segments[0].gapped, true, 'a hole reads as continuous data');
	assert.equal(geometry.segments[1].gapped, false, 'an adjacent pair must not be dashed');
	assert.equal(geometry.segments[1].last, true, 'the newest segment carries the verdict colour');
});

test('a real zero plots as an ordinary value', () => {
	const geometry = spark.sparkGeometry('fixed_overhead_s', [
		snapshot('1', { fixed_overhead_s: 265 }),
		snapshot('2', { fixed_overhead_s: 0 })
	]);

	assert.deepEqual(
		geometry.present.map((p) => p.value),
		[265, 0],
		'a genuine 0 (Modal has no VM to boot) was suppressed as if missing'
	);
	assert.deepEqual(geometry.missing, []);
});

test('the card model agrees with the sparkline on what was measured', () => {
	const snapshots = [
		snapshot('1', { m: 10 }, { m: 'derived' }),
		snapshot('2', { m: null }, {}),
		snapshot('3', { m: 0 }, { m: 'derived' })
	];
	const model = card.buildKpiCard('m', { ...GPU_CAP, direction: 'lower_better' }, snapshots);

	assert.deepEqual(
		model.points.map((p) => p.index),
		spark.sparkGeometry('m', snapshots).present.map((p) => p.index),
		'the big number and the sparkline disagree about which runs measured this KPI'
	);
	assert.equal(model.latest.value, 0, 'a real 0 must be readable as the latest reading');
	assert.equal(model.verdict, 'good', '10 -> 0 on a lower_better KPI is an improvement');
});

test('a KPI measured in no snapshot says so instead of rendering a number', () => {
	const model = card.buildKpiCard('never_measured', GPU_CAP, [
		snapshot('1', { other: 1 }),
		snapshot('2', { other: 2 })
	]);
	assert.equal(model.latest, null);
	assert.equal(model.badge, '');
	assert.match(model.tip.body.join(' '), /Not measured in any of the 2 snapshots/);
});

// ============================================================= M4: formatting

test('a seconds KPI renders in human units and never doubles its unit', () => {
	assert.equal(format.formatDuration(1404), '23m 24s');
	assert.equal(format.formatDuration(265), '4m 25s');
	assert.equal(format.formatValue(1404, 's'), '23m 24s');
	assert.equal(
		format.formatWithUnit(1404, 's'),
		'23m 24s',
		'a duration already carries its unit; appending `s` gives "23m 24s s"'
	);
	assert.ok(format.isDuration('s'));
	assert.ok(!format.isDuration('usd'));
});

test('a sub-minute duration keeps the decimal the bench measures at', () => {
	assert.equal(format.formatDuration(11.1), '11.1s');
	assert.equal(format.formatDuration(5.9), '5.9s');
	assert.equal(format.formatValue(11.1, 's'), '11.1s');
});

test('an unknown direction is a hard error, not a guessed arrow', () => {
	assert.throws(
		() => format.judge(1, 'bigger_is_nicer'),
		/direction must be higher_better or lower_better/,
		'a silent default makes a regression read as an improvement'
	);
	assert.equal(format.judge(0, 'bigger_is_nicer'), 'flat', 'no move is flat whatever the direction');
	assert.equal(format.judge(1, 'higher_better'), 'good');
	assert.equal(format.judge(1, 'lower_better'), 'bad');
	assert.equal(format.judge(-1, 'lower_better'), 'good');
});

test('a card built on an unknown direction throws rather than picking an arrow', () => {
	assert.throws(
		() =>
			card.buildKpiCard('m', { ...GPU_CAP, direction: 'sideways' }, [
				snapshot('1', { m: 1 }),
				snapshot('2', { m: 2 })
			]),
		/direction must be higher_better or lower_better/
	);
});

// ============================================================= M6: fail loudly

test('the ledger fetch rejects a values map that is not an object', () => {
	assert.throws(
		() =>
			kpiApi._parseLedgerForTests({
				kpis: { m: GPU_CAP },
				snapshots: [{ ts: 't', label: 'l', values: [1, 2], provenance: {}, notes: null }]
			}),
		/snapshots\[0\]\.values is not an object/,
		'an array of values must not render as a junk tile of undefined readings'
	);
});

test('the ledger fetch rejects a non-numeric reading', () => {
	assert.throws(
		() =>
			kpiApi._parseLedgerForTests({
				kpis: { m: GPU_CAP },
				snapshots: [{ ts: 't', label: 'l', values: { m: '12' }, provenance: {}, notes: null }]
			}),
		/values\.m is not a number or null/
	);
});

test('the ledger fetch rejects a non-string provenance, which would claim a false origin', () => {
	assert.throws(
		() =>
			kpiApi._parseLedgerForTests({
				kpis: { m: GPU_CAP },
				snapshots: [{ ts: 't', label: 'l', values: { m: 1 }, provenance: { m: 3 }, notes: null }]
			}),
		/provenance\.m is not a string/
	);
});

test('an absent note or provenance loads normally - absence is not corruption', () => {
	const ledger = kpiApi._parseLedgerForTests({
		kpis: { m: GPU_CAP },
		snapshots: [{ ts: 't', label: 'l', values: { m: 1 } }]
	});
	assert.equal(ledger.snapshots[0].notes, null);
	assert.deepEqual(ledger.snapshots[0].provenance, {});
});

test('an unknown direction in the ledger is refused at parse time', () => {
	assert.throws(
		() =>
			kpiApi._parseLedgerForTests({
				kpis: { m: { ...GPU_CAP, direction: 'sideways' } },
				snapshots: [{ ts: 't', label: 'l', values: { m: 1 } }]
			}),
		/direction must be lower_better or higher_better/
	);
});

test('an empty ledger is an error, not an empty grid that looks like "no runs yet"', () => {
	assert.throws(
		() => kpiApi._parseLedgerForTests({ kpis: {}, snapshots: [] }),
		/snapshots is empty/
	);
	assert.throws(
		() => kpiApi._parseLedgerForTests({ kpis: {}, snapshots: {} }),
		/snapshots is not an array/
	);
});

test('an HTTP failure surfaces the status and body, never an empty grid', async () => {
	// openapi-fetch builds a Request before calling fetch; empty VITE_API_BASE
	// is fine in the browser but Node needs an absolute base to construct it.
	const kpiApiNet = await loadTypeScriptModule('src/routes/admin/kpi-api.ts', {
		viteApiBase: 'https://kpi-api.example.test'
	});
	const original = globalThis.fetch;
	globalThis.fetch = async () => new Response('ledger unreadable', { status: 500 });
	try {
		await assert.rejects(
			kpiApiNet.fetchKpiLedger(),
			/GET \/api\/v1\/bench\/kpi failed \(HTTP 500\): ledger unreadable/
		);
	} finally {
		globalThis.fetch = original;
	}
});

// ==================================================== M3 + M5: card structure

test('the whole card is the hover target and answers to the keyboard too', () => {
	const source = readFileSync(`${ADMIN}/KpiTile.svelte`, 'utf8');
	const tooltip = readFileSync(`${ADMIN}/tooltip.svelte.ts`, 'utf8');

	assert.match(
		source,
		/<div class="tile"[^>]*use:tip=/s,
		'the tip action moved off the card root, so only the number is hoverable'
	);
	assert.match(source, /<div class="tile"[^>]*tabindex="0"/s, 'the card is unreachable by keyboard');
	for (const event of ['mouseenter', 'mouseleave', 'focus', 'blur']) {
		assert.ok(
			tooltip.includes(`addEventListener('${event}'`),
			`the tip action no longer binds ${event} - the explainer is mouse-only or sticks`
		);
	}
});

/** Minimal element stand-in: the tip action only ever adds/removes listeners. */
function fakeNode(name) {
	const handlers = new Map();
	return {
		name,
		addEventListener: (type, fn) => handlers.set(type, fn),
		removeEventListener: (type) => handlers.delete(type),
		fire: (type) => {
			const fn = handlers.get(type);
			assert.ok(fn !== undefined, `${name} has no ${type} handler bound`);
			fn();
		},
		bound: () => [...handlers.keys()]
	};
}

test('a tip inside a card restores the card tip when the pointer leaves it', async () => {
	// The pointer never left the card, so the card's own mouseleave never fires.
	// Only a STACK gets the card explainer back after a sparkline dot steals it.
	const tooltip = await loadTypeScriptModule('src/routes/admin/tooltip.svelte.ts');
	const cardNode = fakeNode('card');
	const dotNode = fakeNode('dot');
	const cardHandle = tooltip.tip(cardNode, { title: 'card explainer' });
	const dotHandle = tooltip.tip(dotNode, { title: 'run 3 readout' });

	assert.equal(tooltip.tipState.entry, null, 'a tip shows before anything is hovered');

	cardNode.fire('mouseenter');
	assert.equal(tooltip.tipState.entry.content.title, 'card explainer');

	dotNode.fire('mouseenter');
	assert.equal(tooltip.tipState.entry.content.title, 'run 3 readout', 'the finer target must win');

	dotNode.fire('mouseleave');
	assert.equal(
		tooltip.tipState.entry?.content.title,
		'card explainer',
		'leaving a dot inside the card lost the card explainer - the pointer is still on the card'
	);

	cardNode.fire('mouseleave');
	assert.equal(tooltip.tipState.entry, null, 'leaving the card must clear the tip');

	// Keyboard reaches the same explainer.
	cardNode.fire('focus');
	assert.equal(tooltip.tipState.entry.content.title, 'card explainer');
	cardNode.fire('blur');
	assert.equal(tooltip.tipState.entry, null);

	dotHandle.destroy();
	cardHandle.destroy();
});

test('a destroyed tip does not leave a stale explainer on screen', async () => {
	const tooltip = await loadTypeScriptModule('src/routes/admin/tooltip.svelte.ts');
	const node = fakeNode('card');
	const handle = tooltip.tip(node, { title: 'first' });
	node.fire('mouseenter');
	assert.equal(tooltip.tipState.entry.content.title, 'first');

	handle.update({ title: 'refetched' });
	assert.equal(
		tooltip.tipState.entry.content.title,
		'refetched',
		'a live tip must follow its content across a ledger refetch'
	);

	handle.destroy();
	assert.equal(tooltip.tipState.entry, null, 'an unmounted card left its explainer behind');
	assert.deepEqual(node.bound(), [], 'listeners survived destroy');
});

test('run notes open on the newest run and are untracked against a refetch', () => {
	const source = readFileSync(`${ADMIN}/RunNotes.svelte`, 'utf8');
	assert.match(
		source,
		/untrack\(\(\) => snapshots\.length - 1\)/,
		'the notes either open on run 1 or the reader gets yanked to the newest run mid-read'
	);
	assert.match(
		source,
		/current\.notes \?/,
		'a run with no note must say so rather than render invented commentary'
	);
});

// Perf KPI cards: the second card set, fed by GET /api/v1/bench/perf-kpi.
// - if the perf parse drops `undeclared` then an unlabeled KPI vanishes quietly
// - if the perf section moves above the farm cards or below the ratchet then
//   the dashboard order the maintainer asked for (farm, then perf) is lost

// REQ: PERF-DASH-01
test('the perf ledger parse keeps the start date and the undeclared list', () => {
	const raw = {
		since_label: 'Wed 22 Jul 2026',
		undeclared: ['b'],
		kpis: { a: { label: 'A', unit: 'ms', direction: 'lower_better', title: 'T' } },
		snapshots: [{ ts: '2026-07-22', label: 'Wed 22 Jul 2026 - R1', values: { a: 220 }, notes: 'n' }]
	};
	const parsed = kpiApi._parsePerfLedgerForTests(raw);
	assert.equal(parsed.sinceLabel, 'Wed 22 Jul 2026');
	assert.deepEqual(parsed.undeclared, ['b']);
	assert.equal(parsed.snapshots[0].values.a, 220);
	assert.throws(
		() => kpiApi._parsePerfLedgerForTests({ ...raw, undeclared: undefined }),
		/undeclared/,
		'a missing undeclared list must be an error, not an empty warning'
	);
});

// REQ: PERF-DASH-01
test('perf KPI cards are the second card set: after the farm cards, before the ratchet', () => {
	const source = readFileSync(`${ADMIN}/+page.svelte`, 'utf8');
	const farm = source.indexOf('<h3>Demucs farm KPI ledger</h3>');
	const perf = source.indexOf('<h3>Performance KPIs</h3>');
	const ratchet = source.indexOf('<QualityRatchet />');
	assert.ok(farm > 0 && perf > farm && ratchet > perf, `order farm=${farm} perf=${perf} ratchet=${ratchet}`);
	assert.match(source, /fetchPerfKpiLedger\(\)/);
	assert.match(source, /perf\.sinceLabel/, 'the time period must be stated from the first measurement');
});

// REQ: PERF-DASH-01
test('an undeclared KPI paints an UNDECLARED banner, not a plotted number', () => {
	const source = readFileSync(`${ADMIN}/+page.svelte`, 'utf8');
	assert.match(source, /\{#if perf\.undeclared\.length > 0\}/);
	assert.match(source, /UNDECLARED: \{perf\.undeclared\.join\(', '\)\}/);
});

// PERF-DASH-02: perf sparklines use calendar-date x; farm cards stay index-spaced.

const TODAY = '2026-09-11';

function datedSnapshot(ts, label, values) {
	return { ts, label, values, provenance: {}, notes: null };
}

test('date-axis sparkline: a 28-day gap is about 28 times wider than a 1-day gap', () => {
	const snapshots = [
		datedSnapshot('2026-07-22', 'R1', { m: 10 }),
		datedSnapshot('2026-08-19', 'R2', { m: 11 }),
		datedSnapshot('2026-08-20', 'R3', { m: 12 })
	];
	const geometry = spark.sparkGeometry('m', snapshots, { mode: 'date', today: TODAY });
	const xs = geometry.xs;
	const dx28 = xs[1] - xs[0];
	const dx1 = xs[2] - xs[1];
	assert.ok(Math.abs(dx28 / dx1 - 28) < 1e-6, `28-day vs 1-day ratio was ${dx28 / dx1}, expected ~28`);
	assert.equal(xs[0], spark.SPARK_PAD, 'window starts at the first measurement');
	assert.ok(
		xs[2] < spark.SPARK_W - spark.SPARK_PAD,
		'last point must sit left of the right pad because today is after the last reading'
	);
});

test('date-axis sparkline: a late-only KPI shares the global window, not its own tighter axis', () => {
	const allPresent = spark.sparkGeometry(
		'm',
		[
			datedSnapshot('2026-07-22', 'R1', { m: 10 }),
			datedSnapshot('2026-08-19', 'R2', { m: 11 }),
			datedSnapshot('2026-08-20', 'R3', { m: 12 })
		],
		{ mode: 'date', today: TODAY }
	);
	const lateOnly = spark.sparkGeometry('m', [
		datedSnapshot('2026-07-22', 'R1', { m: null }),
		datedSnapshot('2026-08-19', 'R2', { m: null }),
		datedSnapshot('2026-08-20', 'R3', { m: 12 })
	], { mode: 'date', today: TODAY });
	assert.equal(
		lateOnly.present[0].x,
		allPresent.present[2].x,
		'a late-only KPI must sit at the same x as the third point on the shared window'
	);
});

test('date-axis sparkline: same-day rounds spread within the day instead of stacking', () => {
	const snapshots = [
		datedSnapshot('2026-07-22', 'R1', { m: 10 }),
		datedSnapshot('2026-08-19', 'R2a', { m: 11 }),
		datedSnapshot('2026-08-19', 'R2b', { m: 12 })
	];
	const geometry = spark.sparkGeometry('m', snapshots, { mode: 'date', today: TODAY });
	assert.notEqual(geometry.xs[1], geometry.xs[2], 'same-day rounds must not share x');
	const intraDay = Math.abs(geometry.xs[2] - geometry.xs[1]);
	const oneDay = geometry.xs[1] - geometry.xs[0];
	assert.ok(
		intraDay < oneDay / 10,
		'intra-day spread must be much smaller than a 1-day gap on the same window'
	);
});

test('index-axis sparkline: farm-like ISO timestamps stay equally spaced by default', () => {
	const snapshots = [
		datedSnapshot('2026-07-01T00:00:00Z', 'R1', { m: 10 }),
		datedSnapshot('2026-07-29T00:00:00Z', 'R2', { m: 11 }),
		datedSnapshot('2026-08-26T00:00:00Z', 'R3', { m: 12 })
	];
	const geometry = spark.sparkGeometry('m', snapshots);
	const dx0 = geometry.segments[0].x2 - geometry.segments[0].x1;
	const dx1 = geometry.segments[1].x2 - geometry.segments[1].x1;
	assert.equal(dx0, dx1, 'default mode must keep equal segment spacing');
	for (const point of geometry.present) {
		assert.equal(point.x, spark.sparkX(point.index, 3), 'present x must match sparkX in index mode');
	}
	const dated = spark.sparkGeometry('m', snapshots, { mode: 'date', today: TODAY });
	assert.notEqual(
		dated.present[1].x,
		geometry.present[1].x,
		'date mode must change x when the window ends at today, not at the last snapshot'
	);
});

test('M1 gap geometry still uses index x when no axis is passed', () => {
	const snapshots = [
		snapshot('1', { m: 10 }),
		snapshot('2', { m: null }),
		snapshot('3', { m: 12 })
	];
	const geometry = spark.sparkGeometry('m', snapshots);
	for (const point of geometry.present) {
		assert.equal(point.x, spark.sparkX(point.index, snapshots.length));
	}
});

test('reading age: perf cards show days old with hover title and tip body', () => {
	const kpi = { label: 'Latency', unit: 'ms', direction: 'lower_better', title: 'Deck load time.' };
	const snapshots = [
		datedSnapshot('2026-08-20', 'R1', { deck_load_ms: 220 }),
		datedSnapshot('2026-08-25', 'R2', { deck_load_ms: 210 })
	];
	const model = card.buildKpiCard('deck_load_ms', kpi, snapshots, { timeAxis: true, today: TODAY });
	assert.equal(model.ageDays, 17);
	assert.match(model.ageTitle, /17 days old/);
	assert.match(model.ageTitle, /2026-08-25/);
	assert.match(model.ageTitle, /2026-09-11/);
	assert.match(model.tip.body.join(' '), /17 days old/);
});

test('reading age: measured today shows ageDays 0 and no stale chip', () => {
	const kpi = { label: 'Latency', unit: 'ms', direction: 'lower_better', title: 'Deck load time.' };
	const model = card.buildKpiCard(
		'deck_load_ms',
		kpi,
		[datedSnapshot(TODAY, 'R1', { deck_load_ms: 220 })],
		{ timeAxis: true, today: TODAY }
	);
	assert.equal(model.ageDays, 0);
});

test('reading age: farm cards omit age fields and tip prose', () => {
	const kpi = { label: 'Latency', unit: 'ms', direction: 'lower_better', title: 'Deck load time.' };
	const snapshots = [datedSnapshot('2026-08-25', 'R1', { deck_load_ms: 220 })];
	const model = card.buildKpiCard('deck_load_ms', kpi, snapshots);
	assert.equal(model.ageDays, null);
	assert.equal(model.ageTitle, null);
	assert.ok(!/days old/.test(model.tip.body.join(' ')));
});

test('perf tiles opt into timeAxis; farm tiles do not', () => {
	const page = readFileSync(`${ADMIN}/+page.svelte`, 'utf8');
	assert.match(
		page,
		/<KpiTile[^>]*snapshots=\{ledger\.snapshots\}[^>]*\/>/s,
		'farm tiles must keep ledger snapshots without timeAxis'
	);
	assert.doesNotMatch(
		page.replace(/snapshots=\{ledger\.snapshots\}[\s\S]*?\/>/, ''),
		/snapshots=\{ledger\.snapshots\}[^>]*timeAxis/s
	);
	assert.match(page, /<KpiTile[^>]*snapshots=\{perf\.snapshots\}[^>]*timeAxis/s);
	assert.doesNotMatch(page, /one step per measurement round/);
});

test('KpiTile renders an age chip with title when ageDays is positive', () => {
	const source = readFileSync(`${ADMIN}/KpiTile.svelte`, 'utf8');
	assert.match(source, /class="age"/);
	assert.match(source, /title=\{card\.ageTitle\}/);
	assert.match(source, /ageDays > 0/);
	assert.match(source, /<Sparkline[^>]*\{timeAxis\}/s);
});
