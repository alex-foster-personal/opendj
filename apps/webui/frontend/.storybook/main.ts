import type { StorybookConfig } from '@storybook/sveltekit';

/**
 * Storybook is the visual decision record for this frontend: every
 * pattern-setting UI rule gets a story that demonstrates it plus an MDX
 * decision doc in src/stories/ui-decisions/ that explains it.
 *
 * Stories cover PRESENTATIONAL components only (props in, no fetch). The
 * repo house rule forbids mocked APIs, so a component that fetches is out
 * of scope here rather than wrapped in MSW.
 */
const config: StorybookConfig = {
	stories: ['../src/**/*.mdx', '../src/**/*.stories.@(js|ts)'],
	addons: ['@storybook/addon-docs'],
	framework: '@storybook/sveltekit'
};

export default config;
