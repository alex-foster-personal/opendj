/**
 * The MIDI engine's single on-demand entry point: the WebMIDI/native transport,
 * the builtin device-map registry, the action glue (which brings the takeover
 * policy) and the daemon's installed maps.
 *
 * midi-ui-state.svelte.ts imports THIS module dynamically inside
 * requestMidiAccess(), never statically, so none of it is first-paint weight.
 * One entry rather than four parallel import() calls so Rollup emits the
 * engine as one chunk instead of one chunk per module (each extra chunk costs
 * its own gzip framing and loses shared compression context).
 */
export { initMidi, releaseMidiInputs } from './webmidi.svelte';
export { registerAllDeviceMaps } from './maps';
export { attachMidiGlue } from './action-glue.svelte';
export { loadInstalledDeviceMaps } from './installed-maps';
