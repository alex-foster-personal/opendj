export { noteMasterSilence, resetMasterSilenceWatch } from '$lib/rb/master-silence-report';
export {
	notePositionSample,
	notePresentationClock,
	notePresentationTickFailure,
	readOutputTimestamp,
	resetPresentationClockStall
} from '$lib/rb/presentation-clock-report';
export { awaitPresentedStop, createFrameBackstop, PresentedStopTimeoutError } from '$lib/rb/frame-backstop';
