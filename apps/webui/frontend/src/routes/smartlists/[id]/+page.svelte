<script lang="ts">
	import { page } from '$app/stores';
	import SmartlistRuleEditor from '$lib/components/smartlists/SmartlistRuleEditor.svelte';

	const smartlistId = $derived.by(() => {
		const id = $page.params.id;
		if (id === undefined) throw new Error('smartlists route param "id" missing');
		return id;
	});
</script>

<a href="/smartlists">&larr; back</a>

{#key smartlistId}
	<SmartlistRuleEditor {smartlistId} />
{/key}
