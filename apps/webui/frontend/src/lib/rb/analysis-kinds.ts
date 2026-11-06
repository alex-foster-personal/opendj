/** Analysis kinds and their ribbon colors. Pure data, no store imports, so a props-only view can use it. */
export type AnalysisKind =
	| 'vocals'
	| 'beatgrid'
	| 'key'
	| 'cues'
	| 'waveform'
	| 'phrase'
	| 'loudness'
	| 'stems'
	| 'lyrics'
	| 'load'
	| 'other';

export const ANALYSIS_COLORS: Record<AnalysisKind, string> = {
	vocals: '#5b8cff',
	beatgrid: '#3ecf8e',
	key: '#c9a227',
	cues: '#e87d3e',
	waveform: '#9b7bff',
	phrase: '#2ec4b6',
	loudness: '#f2545b',
	stems: '#adb5bd',
	lyrics: '#c77dff',
	load: '#4cc9f0',
	other: '#868e96'
};
