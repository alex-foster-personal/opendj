<script lang="ts">
	let { message }: { message: string } = $props();
</script>

<div class="banner-warning" role="alert">
	{message}
</div>
