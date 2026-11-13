// Issue #10043: the performance bus and the funnel store in ONE bundle, so the test drives the same store instance.
export * from '$lib/rb/performance-ipc.svelte';
export { trackTableFunnelStore } from '$lib/components/rb/browser/track-table-funnel-store.svelte';
