import { defineVariantSetting } from '$lib/settings/variant-setting';

export const TEST_VARIANT = defineVariantSetting({
	id: 'variant_test_echo',
	label: 'Test echo feedback',
	group: 'advanced',
	keywords: ['variant', 'test'],
	detail: 'Unit-test-only row.',
	variants: [
		{ value: 'a', label: 'A', summary: '0.45' },
		{ value: 'b', label: 'B', summary: '0.55' },
		{ value: 'c', label: 'C', summary: '0.65' }
	],
	defaultValue: 'b'
});
