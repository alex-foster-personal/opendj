<script lang="ts">
	// Tiny reusable spinning-ring glyph, extracted from LibraryLoadIndicator
	// (pin ad59ac) so a second "is something loading" affordance elsewhere
	// (TrackTable's per-deck quick-load box, pin 616aaf77b792) reuses the
	// exact same mark instead of inventing a second spinner design. Colour is
	// currentColor - the caller sets `color` to match its own context.
	let { size = 10 }: { size?: number } = $props();
</script>

<svg class="spinner-icon" viewBox="0 0 16 16" width={size} height={size} aria-hidden="true">
	<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.6" />
	<path d="M8 8V3" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
</svg>

<style>
	.spinner-icon {
		flex: none;
		animation: spinner-icon-spin 0.9s linear infinite;
	}
	@keyframes spinner-icon-spin {
		from {
			transform: rotate(0deg);
		}
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.spinner-icon {
			animation: none;
		}
	}
</style>
