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
	ORDER_BY_OPTIONS
} from '../../src/lib/smartlists/rule-schema.ts';

const REPO_ROOT = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '../../../../..');

function pythonSchema() {
	const script = `
from apps.shared.smartlists.schema import ALLOWED_FIELDS, ALLOWED_OPS_BY_FIELD, FIELD_TYPES
from apps.smartlists.order_by import ALLOWED_ORDER_BY
import json
print(json.dumps({
  "fields": sorted(ALLOWED_FIELDS),
  "field_types": FIELD_TYPES,
  "ops": {k: list(v) for k, v in ALLOWED_OPS_BY_FIELD.items()},
  "order_by": sorted(ALLOWED_ORDER_BY),
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

test('rule-schema order_by options match Python allowlist', () => {
	const py = pythonSchema();
	assert.deepEqual([...ORDER_BY_OPTIONS].sort(), py.order_by);
});
