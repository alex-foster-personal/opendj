// One bundle holding the set-notes store and the shell quit gate, so the
// pending save the test types into is the one the gate flushes (SETNOTE-9951).
export * from '../../../src/lib/set-notes/store.svelte';
export { confirmQuit } from '../../../src/lib/shell/quit-gate';
