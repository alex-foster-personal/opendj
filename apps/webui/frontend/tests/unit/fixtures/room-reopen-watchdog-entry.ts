// One bundle for the room reopen and the context watchdog, so the reopen's
// intentional-suspend excuse is the same one the watchdog reads.
export { reopenRoomOutput } from '$lib/player/headphones-room-reopen';
export { installAudioContextWatchdog, noteRecoveryOpportunity } from '$lib/rb/audio-context-watchdog';
