<script lang="ts">
	// The app-mode chooser tile picture, shared by TopBar and LibraryTopBar.
	// Its paint is component-scoped, so it cannot restyle TrackifyTopBar's tile.
	import { modeIconClass, type AppModeIconId } from '$lib/rb/app-mode-icons';

	let { iconId }: { iconId: AppModeIconId } = $props();
</script>

<span class={`mode-thumbnail ${modeIconClass(iconId)}`} aria-hidden="true"></span>

<style>
	.mode-thumbnail {
		display: block;
		height: 36px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		background-color: #141920;
	}
	.mode-thumbnail.gig {
		background:
			linear-gradient(90deg, transparent 48%, #72b9ff 48% 52%, transparent 52%),
			linear-gradient(#161d26 45%, #72b9ff 45% 52%, #161d26 52%);
	}
	.mode-thumbnail.prep {
		background:
			linear-gradient(180deg, #72b9ff 0 18%, transparent 18% 82%, #346c3e 82% 100%),
			repeating-linear-gradient(90deg, #1a222c 0 4px, #161d26 4px 8px);
	}
	.mode-thumbnail.library {
		background:
			linear-gradient(90deg, #3b79ad 0 22%, transparent 22% 28%, #346c3e 28% 50%, transparent 50% 56%, #7a5c33 56% 78%, transparent 78%),
			#161d26;
	}
	.mode-thumbnail.trackify {
		background:
			radial-gradient(circle at 50% 50%, #a8b2bf 0 12%, #303b48 13% 31%, #72b9ff 32% 36%, #161d26 37%),
			linear-gradient(90deg, transparent 42%, #72b9ff 42% 58%, transparent 58%);
	}
</style>
