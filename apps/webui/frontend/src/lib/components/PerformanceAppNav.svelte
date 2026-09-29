<script lang="ts">
	import { markLibraryModeExit } from '$lib/rb/library-mode-runtime';

	function _navigateLibrary(): void {
		markLibraryModeExit();
	}
</script>

<!-- PERF-UI-02 (issue #2406): compact escape hatch when /performance bypasses the app shell.
     NAV-01: the hatch originally carried only Library + Admin, so every
     library-hygiene tool (missing tracks, duplicate review, smartlists) was
     unreachable from the one screen a DJ actually uses without already
     knowing the bare URL. Extended, not rebuilt: same nav, more links. -->
<nav aria-label="App navigation" data-testid="performance-app-nav" class="performance-app-nav">
	<a
		href="/"
		data-testid="performance-nav-library"
		title="Library"
		onclick={_navigateLibrary}
	>Library</a>
	<a href="/reconcile" data-testid="performance-nav-reconcile" title="Missing tracks">Missing</a>
	<a href="/dedup" data-testid="performance-nav-dedup" title="Duplicate review">Dedup</a>
	<a href="/smartlists" data-testid="performance-nav-smartlists" title="Smartlists">Smartlists</a>
	<a href="/admin" data-testid="performance-nav-admin" title="Admin">Admin</a>
</nav>

<style>
	.performance-app-nav {
		position: fixed;
		left: 0;
		bottom: 0;
		z-index: 50;
		display: flex;
		align-items: center;
		gap: 8px;
		height: 18px;
		/* Rendered width (~214px) must stay under --rb-perf-nav-w (app.css) -
		   BrowserPanel's .bottom-bar reserves that width so its own content
		   (issue #3097's "open dj" wordmark) never renders underneath this
		   fixed overlay. Keep this nav short and single-line; if it grows,
		   grow --rb-perf-nav-w to match and let the overlap e2e prove it. */
		max-width: var(--rb-perf-nav-w);
		padding: 0 8px;
		white-space: nowrap;
		pointer-events: auto;
		background: var(--rb-panel);
		border-top: 1px solid var(--rb-border);
		border-right: 1px solid var(--rb-border);
		color: var(--rb-text-dim);
		font-size: 10px;
	}
	.performance-app-nav a {
		color: inherit;
		text-decoration: none;
	}
	.performance-app-nav a:hover {
		text-decoration: underline;
	}
</style>
