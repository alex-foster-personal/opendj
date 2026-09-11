<script lang="ts">
	/**
	 * /analysis-backfill - the native-analysis v1 backfill queue (NATIVE-10).
	 *
	 * A route of its own rather than a section of an existing page: the queue
	 * is a long-running background job with its own lifecycle, and its panel
	 * polls while it is open. Burying it inside a page people leave open all
	 * day would mean polling all day for a queue nobody is watching.
	 */
	import BackfillQueuePanel from '$lib/components/analysis/BackfillQueuePanel.svelte';
</script>

<svelte:head>
	<title>Analysis backfill queue</title>
</svelte:head>

<BackfillQueuePanel />
