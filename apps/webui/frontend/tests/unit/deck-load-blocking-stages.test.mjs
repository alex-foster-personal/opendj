import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let formatLoadLatency;
let perfLog;
let context;

before(async () => {
	formatLoadLatency = await loadTypeScriptModule('src/lib/rb/format-load-latency.ts');
	context = await loadTypeScriptModule('tests/unit/fixtures/deck-load-context-probe.ts', {
		viteApiBase: 'https://engine.example.test'
	});
	perfLog = context;
});

test('[if] mix-only load stages land [then] canonical blocking KPI keys are numeric, [else stop].', () => {
	const raw = {
		fetchAudio: 220,
		decodeMix: 540,
		stretchCreate: 41,
		stretchLoad: 33,
		processorLatency: 2,
		total: 910
	};
	const snapshot = formatLoadLatency.snapshotDeckLoadBlockingStages(raw);
	assert.equal(snapshot.durations_ms.audioTransfer, 220);
	assert.equal(snapshot.durations_ms.decode, 540);
	assert.equal(snapshot.durations_ms.stretchProcessorCreate, 41);
	assert.equal(snapshot.durations_ms.stretchProcessorLoad, 33);
	assert.equal(snapshot.durations_ms.processorLatency, 2);
	assert.equal(snapshot.durations_ms.total, 910);
	assert.equal(snapshot.stage_sources.decode, 'decodeMix');
});

test('[if] stem stages finish [then] they merge into the deck-load row blocking map, [else stop].', () => {
	const { spanId } = context.beginDeckLoad(1);
	const mix = {
		fetchAudio: 100,
		decodeMix: 200,
		stretchCreate: 10,
		stretchLoad: 5,
		processorLatency: 1,
		total: 400
	};
	context.recordDeckLoad(
		'deck-load sid=abcabcabcabc',
		mix,
		1,
		{
			status: 'loading',
			source: null,
			model: null,
			layout: null,
			error: null
		},
		spanId
	);
	const stem = { probeStem: 9, fetchStems: 80, decodeStems: 120, total: 220 };
	context.supplementDeckLoadKpiWithStemStages(
		1,
		'abcabcabcabc',
		{
			last_load_latency_ms: 400,
			last_load_stages: formatLoadLatency.snapshotDeckLoadBlockingStages(mix).durations_ms,
			last_load_stage_sources: formatLoadLatency.snapshotDeckLoadBlockingStages(mix).stage_sources
		},
		stem
	);
	const row = perfLog
		.readPerfEvents()
		.filter((e) => e.kind.startsWith('deck-load'))
		.at(-1);
	assert.equal(row.blocking_stages.probeStem, 9);
	assert.equal(row.blocking_stages.fetchStems, 80);
	assert.equal(row.blocking_stages.decodeStems, 120);
});

test('[if] a superseded load fails [then] it does not publish last_load_latency_ms, [else stop].', () => {
	const st = {
		last_load_latency_ms: null,
		last_load_stages: null,
		last_load_stage_sources: null
	};
	const published = context.publishDeckLoadKpiFromRaw(st, {
		fetchAudio: 50,
		decodeMix: 50,
		stretchCreate: 5,
		stretchLoad: 5,
		processorLatency: 1,
		total: 155
	});
	assert.equal(st.last_load_latency_ms, 155);
	assert.equal(published.durations_ms.total, 155);
	assert.ok(!('fetchAudio' in st.last_load_stages));
});

test('[if] client deck sample is built [then] duration map excludes kpi_src labels, [else stop].', async () => {
	const samples = await loadTypeScriptModule('src/lib/rb/client-performance-samples.ts', {
		viteApiBase: 'https://engine.example.test'
	});
	const row = samples.deckPerformanceSampleFromState(1, {
		stable_id: 'sid',
		duration_ms: 120_000,
		playing: false,
		audible: false,
		transport_pending: false,
		stems: { status: 'ready' },
		last_load_latency_ms: 800,
		last_load_stages: { total: 800, decode: 200, audioTransfer: 100 },
		last_load_stage_sources: { decode: 'decodeMix', audioTransfer: 'fetchAudio' },
		sync_error: null,
		processor_error: null
	});
	assert.equal(row.last_load_latency_ms, 800);
	assert.equal(row.last_load_blocking_stage_ms.total, 800);
	assert.equal(row.last_load_stage_sources.decode, 'decodeMix');
	assert.equal(row.last_load_blocking_stage_ms.kpi_src_decode, undefined);
});
