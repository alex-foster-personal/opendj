<script lang="ts">
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { paintSectionsPreview } from '$lib/settings/sections-preview-paint';
	import SectionDisplayPreviewView from './SectionDisplayPreviewView.svelte';

	let { label = 'Segment display preview' }: { label?: string } = $props();

	const prefs = $derived({
		show_sections: uiPrefs.show_sections,
		sections_main_style: uiPrefs.sections_main_style,
		sections_sticky_title: uiPrefs.sections_sticky_title,
		sections_next_countdown: uiPrefs.sections_next_countdown,
		sections_overview_style: uiPrefs.sections_overview_style,
		sections_library_structure_column: uiPrefs.sections_library_structure_column,
		sections_preview_cursor_label: uiPrefs.sections_preview_cursor_label,
		sections_uncertain: uiPrefs.sections_uncertain
	});
</script>

<SectionDisplayPreviewView paint={paintSectionsPreview} {prefs} {label} />
