/**
 * StemsPrompt first-run presentation: human copy vs agent diagnostics.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function read(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

test('stems errors keep endpoints and refusal codes in agent disclosures', () => {
	const source = read('src/lib/components/rb/StemsPrompt.svelte');
	assert.match(source, /humanStemsJobsUnavailable\(\)/);
	assert.match(source, /humanStemsPlanLoadError\(\)|humanStemsPlanTimeout\(\)/);
	assert.match(source, /humanStemsBlocked\(localExecutor\)/);
	assert.match(source, /humanStemsEnqueueError\(\)/);
	assert.match(source, /AGENT_DETAILS_LABEL/);
	assert.match(source, /data-agent-stems-refusal=\{refusal\}/);
	assert.match(source, /data-agent-stems-load-error=\{loadError\}/);
	assert.match(source, /data-agent-stems-blocked=\{blocked\}/);
	assert.match(source, /data-agent-stems-enqueue-error=\{enqueueError\}/);
	assert.doesNotMatch(source, /title=\{refusal\}/);
	assert.doesNotMatch(source, /title=\{loadError\}/);
	assert.doesNotMatch(source, /title=\{blocked\}/);
	assert.doesNotMatch(source, /Could not start: \{enqueueError\}/);
	assert.doesNotMatch(
		source,
		/<p class="stems-error"[^>]*>[^<]*GET \/api\/v1\/stems\/plan/
	);
});
