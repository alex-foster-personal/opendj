import type { PlaylistSummaryStatsDoc } from './api-playlist-summary';

export function formatDurationHms(durationS: number): string {
	const total = Math.round(durationS);
	const h = Math.floor(total / 3600);
	const m = Math.floor((total % 3600) / 60);
	if (h > 0) return m > 0 ? `${h} h ${m} m` : `${h} h`;
	if (m > 0) return `${m} m`;
	return `${total} s`;
}

export function formatPlaylistSummaryLine(stats: PlaylistSummaryStatsDoc): string {
	// LIBM-105b: an empty list has no duration, BPM or keys to report.
	if (stats.tracks === 0) return '0 tracks';
	const parts: string[] = [`${stats.tracks} tracks`, formatDurationHms(stats.duration_s)];
	const { bpm, energy, keys } = stats;
	const tracksTip = `${stats.tracks} distinct playlist members`;
	if (bpm.min !== null && bpm.max !== null) {
		const med = bpm.median !== null ? ` (${Math.round(bpm.median)})` : '';
		parts.push(`${Math.round(bpm.min)} to ${Math.round(bpm.max)} BPM${med}`);
	}
	if (energy.min !== null && energy.max !== null) {
		parts.push(`energy ${energy.min} to ${energy.max}`);
	}
	parts.push(`${keys.distinct} keys`);
	if (stats.unanalysed > 0) parts.push(`${stats.unanalysed} unanalysed`);
	if (stats.unavailable > 0) parts.push(`${stats.unavailable} unavailable`);
	return parts.join(', ');
}

export function playlistSummaryTitle(stats: PlaylistSummaryStatsDoc): string {
	return [
		`${stats.tracks} tracks: ${tracksDenominator(stats)}`,
		`running time from ${stats.tracks} members (seconds ${stats.duration_s.toFixed(1)})`,
		`BPM range over ${stats.tracks - stats.bpm.missing} of ${stats.tracks} with BPM`,
		`energy over ${stats.tracks - stats.energy.missing} of ${stats.tracks}`,
		`${stats.keys.distinct} distinct keys (${stats.keys.missing} missing)`,
		`${stats.unanalysed} missing BPM or key of ${stats.tracks}`,
		`${stats.unavailable} with audio missing on this machine of ${stats.tracks}`
	].join(' · ');
}

function tracksDenominator(stats: PlaylistSummaryStatsDoc): string {
	return `${stats.tracks} distinct stable_ids`;
}
