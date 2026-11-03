import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import { astToForm, createEmptyGroup } from '$lib/smartlists/rule-form';
import type { FormGroup } from '$lib/smartlists/rule-form';

import RuleGroup from './RuleGroup.svelte';

/**
 * The group prop is the form model from $lib/smartlists/rule-form, built
 * with the real astToForm / createEmptyGroup over invented rules. The editor
 * mutates this object in place, so in Storybook the + predicate / + group
 * buttons mutate the arg without re-rendering (the app's parent owns the
 * reactive state); stories show the static shape of each state.
 */
const group = (ast: Parameters<typeof astToForm>[0]) => astToForm(ast) as FormGroup;

const meta = {
	title: 'Components/Smartlists/RuleGroup',
	component: RuleGroup,
	tags: ['autodocs'],
	args: { onRemove: fn() }
} satisfies Meta<typeof RuleGroup>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Empty AND group: shows the "requires at least one child" error. */
export const Empty: Story = { args: { group: createEmptyGroup('and') } };

/** AND group with two predicates. */
export const Flat: Story = {
	args: {
		group: group({
			op: 'and',
			children: [
				{ field: 'genre', op: '=', value: 'Deep House' },
				{ field: 'bpm', op: 'between', value: [118, 126] }
			]
		})
	}
};

/** Nested groups: OR inside AND, each with its own remove button. */
export const Nested: Story = {
	args: {
		group: group({
			op: 'and',
			children: [
				{ field: 'rating', op: '>=', value: 4 },
				{
					op: 'or',
					children: [
						{ field: 'key', op: 'in', value: ['8A', '9A'] },
						{ field: 'added_date', op: '>', value: { $relative: '-2w' } }
					]
				}
			]
		})
	}
};

/** NOT with its one child: the add buttons are hidden. */
export const NotGroup: Story = {
	args: {
		group: group({ op: 'not', children: [{ field: 'color_tag', op: '=', value: 'red' }] })
	}
};

/** No onRemove: the root group has no "remove group" button. */
export const Root: Story = {
	args: { group: createEmptyGroup('or') },
	render: (args) => ({ Component: RuleGroup, props: { group: args.group } })
};
