/**
 * One bundle for the hotkey session recovery tests (#12289): the #12275
 * rejection bundle (real hotkey listener, real dispatcher) plus the pending
 * load-play store, so a Space press and the pending intent it edits are the
 * same module instances.
 */
export * from './performance-hotkey-rejection-entry';
export { beginPendingLoadPlay, pendingLoadPlayState } from '$lib/rb/deck-slots';
