<script lang="ts">
	import { onMount } from 'svelte';
	import TrackifyPlayer from '$lib/components/rb/TrackifyPlayer.svelte';
	import { installTrackifySession } from '$lib/rb/trackify-session.svelte';

	onMount(() => installTrackifySession());
</script>

<svelte:head>
	<title>Open DJ - Trackify</title>
</svelte:head>

<TrackifyPlayer />
