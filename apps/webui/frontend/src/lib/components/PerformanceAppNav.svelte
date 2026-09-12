<!-- PERF-UI-02 (issue #2406): compact escape hatch when /performance bypasses the app shell. -->
<nav aria-label="App navigation" data-testid="performance-app-nav" class="performance-app-nav">
	<a href="/" data-testid="performance-nav-library" title="Library">Library</a>
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
