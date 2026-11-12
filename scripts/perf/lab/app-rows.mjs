// perf-lab: collect the app's own staged model rows (perf-event-log.ts) for
// every latency press.
//
// The app keeps them in a bounded ring (a handful per bucket), so reading it
// once after a run of hundreds of presses sees only the last few, and an L4
// coverage ratio computed over those survivors can read 1.0 while almost every
// press went unmeasured. The driver harvests after EACH press instead, and the
// rows the ring held before the scenario started are seeded as seen, so they
// never count as this scenario's.

/** Whether a perf-event row carries the press-to-audible model stages. */
export function isModelRow(r) {
	return Boolean(r && r.stages && ('input_to_audible_ms' in r.stages || 'input_to_output_ms' in r.stages));
}

function rowKey(r) {
	return `${r.t}|${r.kind}|${r.deck}|${r.id ?? ''}|${r.message}`;
}

/** A collector: seed(ring) marks rows already present; take(ring, action)
 * keeps the model rows not seen before, in ring order, tagged perflab_action.
 * rows holds every one taken. */
export function modelRowCollector() {
	const seen = new Set();
	const rows = [];
	return {
		rows,
		seed(ring) {
			for (const r of ring ?? []) seen.add(rowKey(r));
		},
		take(ring, action = null) {
			for (const r of ring ?? []) {
				const k = rowKey(r);
				if (seen.has(k)) continue;
				seen.add(k);
				// tagged with the press it followed, so one action's rows can
				// never stand in for another's missing ones
				if (isModelRow(r)) rows.push(action === null ? r : { ...r, perflab_action: action });
			}
		}
	};
}
