// One bundle holding the USB export store and the shell quit gate, so the
// veto the store registers at load is the one the gate consults.
export * from '../../../src/lib/rb/usb-export.svelte';
export { quitVetoed } from '../../../src/lib/shell/quit-gate-state';
