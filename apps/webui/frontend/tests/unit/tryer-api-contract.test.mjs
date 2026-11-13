// requirement: TRYER-14
// The try-er panel reads its documents through types generated from apps/webui/openapi.json; this pins the fields it
// consumes there (tests/webui/test_dj_try_routes.py pins openapi.json against the live route).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SCHEMAS = JSON.parse(readFileSync(new URL('../../../openapi.json', import.meta.url), 'utf8'))
	.components.schemas;

// Every field each try schema declares: a route that adds or removes one fails here until the panel is reviewed.
const DECLARED = {
	TryJobOut: [
		'best',
		'best_reason',
		'candidates',
		'context_bars',
		'created_at',
		'etag',
		'in',
		'job_id',
		'n',
		'out',
		'pick',
		'scorer_version'
	],
	CandidateOut: [
		'bars',
		'energy',
		'factors',
		'id',
		'measured',
		'metrics',
		'plan',
		'plan_sha256',
		'planner_confidence',
		'planner_confidence_title',
		'planner_weakest_factor',
		'rank_group',
		'refused',
		'render_ms',
		'render_url',
		'score',
		'score_ms',
		'scorer_version',
		'style',
		'unknown',
		'vetoes'
	],
	PickOut: [
		'created_at',
		'in_id',
		'job_id',
		'out_id',
		'picked_candidate_id',
		'plan',
		'plan_sha256',
		'rejected_ids',
		'scorer_version'
	],
	VetoOut: ['name', 'threshold', 'value'],
	MeasuredOut: ['k', 'n'],
	TryLanesOut: ['candidate', 'decks', 'duration_s', 'energy'],
	DeckLaneOut: ['beats', 'curves', 'deck', 'peaks', 'playing']
};

// The fields the panel, its model and its client read: [nullable, required].
const CONSUMED = {
	TryJobOut: {
		job_id: [false, true],
		etag: [false, true],
		scorer_version: [false, true],
		context_bars: [false, true],
		candidates: [false, true],
		best: [true, true],
		best_reason: [false, true],
		pick: [true, true]
	},
	CandidateOut: {
		id: [false, true],
		style: [false, true],
		bars: [false, true],
		score: [true, true],
		score_ms: [false, true],
		render_ms: [true, true],
		refused: [true, true],
		render_url: [false, true],
		rank_group: [false, true],
		measured: [false, true],
		vetoes: [false, true],
		unknown: [false, true],
		energy: [false, true],
		planner_confidence: [true, false],
		planner_weakest_factor: [true, false],
		planner_confidence_title: [true, false]
	},
	PickOut: { picked_candidate_id: [false, true] },
	VetoOut: { name: [false, true], value: [false, true], threshold: [false, true] },
	MeasuredOut: { k: [false, true], n: [false, true] },
	TryLanesOut: { duration_s: [false, true], decks: [false, true] },
	DeckLaneOut: {
		deck: [false, true],
		peaks: [false, true],
		curves: [false, true],
		playing: [false, true],
		beats: [false, true]
	}
};

function nullable(property) {
	return (property.anyOf ?? []).some((branch) => branch.type === 'null');
}

let model;

before(async () => {
	model = await loadTypeScriptModule('src/lib/tryer/tryer-model.ts');
});

test('[if] the candidate score is null for insufficient measurements [then] the schema declares it required and nullable and the bar is not drawn, [else stop]', () => {
	const candidate = SCHEMAS.CandidateOut;
	assert.ok(
		candidate.required.includes('score'),
		'if score is optional then an absent score reads as 0'
	);
	assert.ok(
		nullable(candidate.properties.score),
		'if score is not nullable then unscored candidates lie'
	);
	const [bounded] = candidate.properties.score.anyOf.filter((branch) => branch.type === 'number');
	assert.deepEqual([bounded.minimum, bounded.maximum], [0, 1]);
	assert.equal(
		model.scoreBarPercent({ id: 'N', score: null }),
		null,
		'if null draws a bar then unscored reads as 0'
	);
});

test('[if] a candidate has no kept render path [then] the schema hides render_path, keeps render_url a string and render_ms nullable, [else stop]', () => {
	const { properties, required } = SCHEMAS.CandidateOut;
	assert.equal(
		properties.render_path,
		undefined,
		'if render_path is public then a server path leaks'
	);
	assert.equal(properties.render_url.type, 'string');
	assert.ok(required.includes('render_url') && !nullable(properties.render_url));
	assert.ok(
		nullable(properties.render_ms) && nullable(properties.refused),
		'the panel shows "not rendered" on null'
	);
});

test('[if] the route adds or removes a panel-consumed field [then] the committed schema differs from the pinned shape, [else stop]', () => {
	for (const [name, fields] of Object.entries(DECLARED)) {
		const schema = SCHEMAS[name];
		assert.ok(schema, `${name} is missing from openapi.json`);
		assert.deepEqual(Object.keys(schema.properties).sort(), fields, `${name} fields changed`);
		for (const [field, [isNullable, isRequired]] of Object.entries(CONSUMED[name])) {
			assert.equal(nullable(schema.properties[field]), isNullable, `${name}.${field} nullability`);
			assert.equal(
				(schema.required ?? []).includes(field),
				isRequired,
				`${name}.${field} required`
			);
		}
	}
});
