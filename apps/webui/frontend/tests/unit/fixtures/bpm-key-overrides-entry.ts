// One bundle holding the cell-commit module and its stubbed dispatcher, so the
// test reads the same stub instance the module calls.
export { commitBpmEdit, commitKeyEdit, editedKey } from '../../../src/lib/components/rb/browser/bpm-key-overrides.svelte';
export { answerWith, sent } from './bpm-key-overrides-stub-ipc';
