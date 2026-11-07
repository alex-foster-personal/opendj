/**
 * Contract: `CustomiseScreen` is a second view over `SETTINGS_CATALOG` through
 * `readSettingValue`/`applySettingChange`. Run:
 *   pnpm test:contracts src/lib/components/rb/customise/CustomiseScreen.stories.ts
 */
import type { Meta, StoryObj } from '@storybook/sveltekit';
import { http, HttpResponse } from 'msw';
import { expect, userEvent, waitFor, within } from 'storybook/test';

import { uiPrefs } from '$lib/rb/prefs.svelte';
import { SETTINGS_CATALOG } from '$lib/settings/catalog';
import { customiseScreenDef, customiseSettingIds } from '$lib/settings/customise-screens';

import CustomiseScreen from './CustomiseScreen.svelte';

const meta = {
	title: 'Contracts/Customise/CustomiseScreen',
	component: CustomiseScreen,
	parameters: {
		msw: {
			handlers: [http.put('/api/v1/ui-prefs', () => HttpResponse.json({}))]
		}
	}
} satisfies Meta<typeof CustomiseScreen>;

export default meta;
type Story = StoryObj<typeof meta>;

const DECKS = customiseScreenDef('decks');

export const Decks: Story = {
	args: { def: DECKS },
	play: async ({ canvasElement }) => {
		expect(canvasElement.querySelector('[data-customise-screen="decks"]')).not.toBeNull();
		const ids = customiseSettingIds(DECKS);
		expect(ids.length).toBe(12);
		for (const id of ids) {
			const entry = SETTINGS_CATALOG.find((d) => d.id === id);
			expect(entry).toBeDefined();
			const row = canvasElement.querySelector(`[data-customise-row="${id}"]`);
			expect(row).not.toBeNull();
			const text = row?.textContent ?? '';
			expect(text).toContain(entry!.label);
			expect(text).toContain(entry!.detail);
		}
		expect(canvasElement.querySelectorAll('[data-customise-row]').length).toBe(12);
	}
};

export const DecksRightMirrorToggle: Story = {
	args: { def: DECKS },
	play: async ({ canvasElement }) => {
		expect(canvasElement.querySelector('[data-customise-screen="decks"]')).not.toBeNull();
		const before = uiPrefs.deck_right_mirror;
		const mirrorEntry = SETTINGS_CATALOG.find((d) => d.id === 'deck_right_mirror');
		expect(mirrorEntry).toBeDefined();
		const toggle = within(canvasElement).getByRole('switch', { name: mirrorEntry!.label });
		await userEvent.click(toggle);
		await waitFor(() => expect(uiPrefs.deck_right_mirror).toBe(!before));
		expect(toggle).toHaveProperty('checked', !before);
		await userEvent.click(toggle);
		await waitFor(() => expect(uiPrefs.deck_right_mirror).toBe(before));
		expect(toggle).toHaveProperty('checked', before);
	}
};
