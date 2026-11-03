/** BPM column hover copy (LIBUX-21). */
import type { BrowserRow } from '$lib/components/rb/browser/pane-contract.svelte';
import { bpmHeatLabel, classifyBpmHeat } from '$lib/rb/bpm-heat';
import { hasUsableBpm } from '$lib/rb/bpm-cell-text';

/** Cell text for an analyzed track with no tempo (issue #5986): never a 0. */
export function bpmUnmeasuredLabel(status: 'too_short' | 'no_pulse'): string {
	return status === 'too_short' ? 'too short' : 'no pulse';
}

export function bpmCellTitle(row: BrowserRow, masterBpm: number | null): string {
	if (row.bpm_status === 'failed') {
		return row.bpm_reason ?? 'bpm analysis failed';
	}
	if (row.bpm_status === 'missing') {
		return row.bpm_reason ?? 'bpm not analyzed yet';
	}
	if (row.bpm_status === 'available-not-selected') {
		return row.bpm_reason ?? 'beatgrid analysis available but not selected';
	}
	if (row.bpm_status === 'too_short' || row.bpm_status === 'no_pulse') {
		return row.bpm_reason ?? bpmUnmeasuredLabel(row.bpm_status);
	}
	const parts: string[] = [];
	const heat = bpmHeatLabel(classifyBpmHeat(row.bpm, masterBpm), masterBpm);
	if (heat !== null) parts.push(heat);
	if (row.bpm_method) {
		parts.push(`Beat grid: ${row.bpm_method}`);
	} else if (row.bpm_source) {
		parts.push(`Beat grid source: ${row.bpm_source}`);
	}
	if (row.bpm_confidence !== null && row.bpm_confidence !== undefined) {
		const pct = Math.round(row.bpm_confidence * 100);
		parts.push(`Confidence: ${pct}%`);
	} else if (row.bpm_confidence_error) {
		parts.push(`Confidence: ${row.bpm_confidence_error}`);
	}
	if (hasUsableBpm(row.bpm)) {
		parts.push(`Exact BPM: ${row.bpm.toFixed(1)}`);
		if (row.bpm_status === 'ok' && !row.bpm_method && row.bpm_confidence == null && !row.bpm_confidence_error) {
			parts.push('Dynamic tempo analysis: not analyzed');
		}
	}
	if (parts.length === 0) {
		return 'BPM not analyzed';
	}
	return parts.join('. ') + '.';
}
