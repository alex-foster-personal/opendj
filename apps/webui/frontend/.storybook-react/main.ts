import type { StorybookConfig } from '@storybook/react-vite';

/**
 * The React Storybook: one React twin per Svelte component, each next to its
 * original (X.tsx beside X.svelte) with an X.stories.tsx that reuses the
 * Svelte story's X.contract.ts, so both Storybooks show the same story ids.
 * The Svelte Storybook (.storybook, port 6006) composes this one (port 6007)
 * as the "React twins" ref; `pnpm story:parity` diffs every pair.
 * Plan: https://claude.ai/artifact/EuqJD1rYELTpnCy8EReCuc
 */
const config: StorybookConfig = {
	stories: ['../src/**/*.stories.tsx'],
	addons: ['@storybook/addon-docs', '@storybook/addon-vitest'],
	framework: {
		name: '@storybook/react-vite',
		options: { builder: { viteConfigPath: 'vite.react.config.ts' } }
	},
	staticDirs: ['../.storybook/msw']
};

export default config;
