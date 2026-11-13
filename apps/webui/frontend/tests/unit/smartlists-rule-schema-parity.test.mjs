/**
 * Parity between browser rule-schema.ts and apps/shared/smartlists/schema.py.
 */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import path from 'node:path';
import {
	ALLOWED_FIELDS,
	ALLOWED_OPS_BY_FIELD,
	FIELD_TYPES,
	ORDER_BY_OPTIONS,
	TOP_N_OP,
	TOP_N_SORT_FIELDS
} from '../../src/lib/smartlists/rule-schema.ts';

const REPO_ROOT = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '../../../../..');

function pythonSchema() {
	const script = `
from apps.shared.smartlists.schema import ALLOWED_FIELDS, ALLOWED_OPS_BY_FIELD, FIELD_TYPES
from apps.smartlists.order_by import ALLOWED_ORDER_BY
from apps.shared.smartlists.schema import TOP_N_OP, TOP_N_SORT_FIELDS
import json
print(json.dumps({
  "fields": sorted(ALLOWED_FIELDS),
  "field_types": FIELD_TYPES,
  "ops": {k: list(v) for k, v in ALLOWED_OPS_BY_FIELD.items()},
  "order_by": sorted(ALLOWED_ORDER_BY),
  "top_n": {"op": TOP_N_OP, "sort_fields": sorted(TOP_N_SORT_FIELDS)},
}))
`;
	const result = spawnSync('uv', ['run', 'python', '-c', script], {
		cwd: REPO_ROOT,
		encoding: 'utf8'
	});
	assert.equal(
		result.status,
		0,
		(result.stderr || result.stdout || 'python schema dump failed').trim()
	);
	return JSON.parse(result.stdout);
}

const NEW_FIELDS = ['play_count', 'duration_s', 'playlist_count'];

test('rule-schema exposes play_count, duration_s, playlist_count like Python', () => {
	const py = pythonSchema();
	for (const field of NEW_FIELDS) {
		assert.ok(py.fields.includes(field), `python missing ${field}`);
		assert.ok(ALLOWED_FIELDS.includes(field), `typescript missing ${field}`);
		assert.equal(FIELD_TYPES[field], py.field_types[field]);
		assert.deepEqual([...ALLOWED_OPS_BY_FIELD[field]].sort(), [...py.ops[field]].sort());
	}
});

const TEXT_FIELDS = ['title', 'artist', 'album', 'comments'];

test('rule-schema exposes title, artist, album, comments as string fields like Python', () => {
	const py = pythonSchema();
	for (const field of TEXT_FIELDS) {
		assert.ok(py.fields.includes(field), `python missing ${field}`);
		assert.ok(ALLOWED_FIELDS.includes(field), `typescript missing ${field}`);
		assert.equal(FIELD_TYPES[field], 'string');
		assert.equal(py.field_types[field], 'string');
		assert.deepEqual([...ALLOWED_OPS_BY_FIELD[field]].sort(), [...py.ops[field]].sort());
		assert.deepEqual([...py.ops[field]].sort(), ['!=', '=', 'contains', 'in', 'missing']);
	}
});

test('rule-schema order_by options match Python allowlist', () => {
	const py = pythonSchema();
	assert.deepEqual([...ORDER_BY_OPTIONS].sort(), py.order_by);
});

test('[if] the top-N op or its sort fields drift from Python [then] this parity check fails, [else stop].', () => {
	const py = pythonSchema();
	assert.equal(TOP_N_OP, py.top_n.op);
	assert.deepEqual([...TOP_N_SORT_FIELDS].sort(), py.top_n.sort_fields);
});
