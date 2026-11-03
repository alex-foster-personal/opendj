<script lang="ts">
	// The topbar's status chips: CloudSync, then whatever installed modules
	// draw in the `topbar.end` slot (PLUG-6522i). One component out of TopBar so
	// the topbar's import fan-out stays within the quality ratchet, the same
	// reason TopBarAccountCluster exists.
	import CloudSyncStatusChip from '$lib/components/CloudSyncStatusChip.svelte';
	import PluginSlot from '$lib/plugins/PluginSlot.svelte';
</script>

<CloudSyncStatusChip />
<!-- Installed modules (B2B Link's status chip first) draw here. Empty without plugins. -->
<PluginSlot name="topbar.end" />
