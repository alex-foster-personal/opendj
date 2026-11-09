import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC_ROOT = fileURLToPath(new URL('../../src', import.meta.url));

/** The `type: '...'` literals of the PerformanceCommand union, read from source. */
export function performanceCommandTypes() {
	const source = readFileSync(join(SRC_ROOT, 'lib/rb/performance-ipc-types.ts'), 'utf8');
	const start = source.indexOf('export type PerformanceCommand =');
	assert.ok(start >= 0, 'PerformanceCommand union not found in performance-ipc-types.ts');
	const end = source.indexOf('\nexport ', start + 1);
	const block = source.slice(start, end);
	return new Set([...block.matchAll(/type: '([a-z0-9_]+)'/g)].map((m) => m[1]));
}
