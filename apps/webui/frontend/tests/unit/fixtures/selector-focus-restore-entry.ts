/** One bundle so the focus restorer and the `selectorNode` action share one node registry (#13428). */
export {
	installSelectorFocusRestore,
	markSelectorFocus,
	selectorFocusRestoreNode
} from '$lib/keyboard/selector-focus-restore';
export { selectorNode } from '$lib/keyboard/selector-register';
