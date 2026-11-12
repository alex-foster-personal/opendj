// The Node a lab script runs under, held to the frontend's engines.node floor
// (the same rule as scripts/perf/node_runtime.py): a capture from an older
// runtime would publish trace, latency and heap evidence the app never runs on.
import fs from 'node:fs';

// Why `version` (process.versions.node) is refused under the floor in
// `packageJson`, or null when it is at or above it.
export function nodeFloorRefusal(version, packageJson) {
	const spec = JSON.parse(fs.readFileSync(packageJson, 'utf8')).engines?.node;
	const floor = /^>=(\d+)\.(\d+)\.(\d+)$/.exec(spec ?? '');
	if (!floor) return `${packageJson} engines.node is ${JSON.stringify(spec)}, not a >= floor`;
	const have = /^v?(\d+)\.(\d+)\.(\d+)/.exec(version);
	if (!have) return `not a Node version: ${JSON.stringify(version)}`;
	for (let i = 1; i <= 3; i++) {
		if (Number(have[i]) !== Number(floor[i])) {
			return Number(have[i]) < Number(floor[i]) ? `Node ${version} is below the frontend floor ${spec.slice(2)}` : null;
		}
	}
	return null;
}
