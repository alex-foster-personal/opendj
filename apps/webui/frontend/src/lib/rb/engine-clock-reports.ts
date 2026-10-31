export { noteMasterSilence, resetMasterSilenceWatch } from '$lib/rb/master-silence-report';
export {
	notePresentationClock,
	notePresentationTickFailure,
	presentationTickFailure,
	readOutputTimestamp,
	resetPresentationClockStall
} from '$lib/rb/presentation-clock-report';
export { notePositionSample, presentedSampleAtMs } from '$lib/rb/playhead-display.svelte';
export { awaitPresentedStop, createFrameBackstop, PresentedStopTimeoutError } from '$lib/rb/frame-backstop';
// AGENT-30: the audio-clock half of a plan step (a scheduled set, and the presented playhead it is timed from).
export { ScheduledSteps } from '$lib/rb/scheduled-mixer-write';
export { validAnchor, type PositionAnchor, type RecordedAnchor } from '$lib/rb/position-anchor';
