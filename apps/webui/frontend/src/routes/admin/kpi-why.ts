/**
 * Why each KPI matters for the demucs iteration loop.
 *
 * Presentation-layer copy, deliberately NOT in scripts/bench/kpi_ledger.json:
 * that file is a thin, machine-appended data file (see kpi_append.py). This is
 * appended to the ledger's own kpi.title in the card's hover explainer.
 */
export const KPI_WHY: Record<string, string> = {
	iter_latency_10trk_s:
		'This is the number that made small iterations painfully slow on the GCP Spot VM path, ' +
		'since a 265s VM boot dominates a batch this small.',
	iter_latency_100trk_s:
		'At this size, fixed overhead is a smaller slice of the total, so this reflects real ' +
		'per-track throughput more than a small batch does.',
	fixed_overhead_s: 'This is what made small 10-track iterations painfully slow.',
	per_track_wall_s:
		'This is the number that actually matters once a run is big enough to amortise startup, ' +
		'and the one to watch when comparing a 1-track spike against a fanned-out run.',
	separate_p50_s:
		'This isolates whether the model itself got faster or slower, independent of infrastructure.',
	model_load_per_track_s:
		'A cold container pays this in full, a warm or fanned-out one amortises it near zero, ' +
		'which explains most of the gap between a single-track spike and a fan-out run.',
	cost_per_track_usd:
		'This decides whether farming the remaining backlog (over a thousand tracks) is affordable.',
	max_parallel_gpus:
		'More parallel GPUs clears the backlog faster; GCP capped this at a shared per-project ' +
		'quota bucket, Modal does not.',
	tracks_cached_total:
		'This is the actual progress metric against the library backlog, independent of how fast ' +
		'any single run was.',
	ops_incidents_per_run:
		'Each incident is wall-clock time lost babysitting infrastructure instead of separating tracks.',
	si_sdr_db:
		'This is the quality half of the speed/quality tradeoff the ladder exists to measure, so a ' +
		'speed win only counts if this does not collapse.',
	region_iou:
		'This measures whether the PreviewStrip vocal bars land in the right place, separate from ' +
		'stem audio quality.',
	human_quality_1to10:
		'This is the ground-truth check against si_sdr_db and region_iou, since neither automated ' +
		'metric perfectly tracks what a listener hears.',
	si_sdr_true_db:
		'This is the only quality number that survives comparison across runs, because it is scored ' +
		'against a real isolated vocal stem rather than against our own best output.'
};
