import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import { astToForm } from '$lib/smartlists/rule-form';
import type { FormPredicate } from '$lib/smartlists/rule-form';

import RulePredicate from './RulePredicate.svelte';

/**
 * The predicate prop is the form model from $lib/smartlists/rule-form,
 * built here with the real astToForm over invented rules. The editor binds
 * straight into this object, so in Storybook the inputs accept typing but
 * Svelte may warn that the arg is not reactive state; the parent
 * (RuleGroup / the editor) owns reactivity in the app.
 */
const pred = (ast: Parameters<typeof astToForm>[0]) => astToForm(ast) as FormPredicate;

const meta = {
	title: 'Components/Smartlists/RulePredicate',
	component: RulePredicate,
	tags: ['autodocs'],
	args: { onRemove: fn() }
} satisfies Meta<typeof RulePredicate>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Single-value operator on a string field. */
export const SingleValue: Story = {
	args: { predicate: pred({ field: 'genre', op: '=', value: 'Deep House' }) }
};

/** 'between' shows lower and upper bound inputs. */
export const Between: Story = {
	args: { predicate: pred({ field: 'bpm', op: 'between', value: [118, 126] }) }
};

/** 'in' shows an editable value list. */
export const InList: Story = {
	args: { predicate: pred({ field: 'key', op: 'in', value: ['8A', '9A', '10A'] }) }
};

/** Date fields show the relative-date hint. */
export const RelativeDate: Story = {
	args: { predicate: pred({ field: 'added_date', op: '>', value: { $relative: '-7d' } }) }
};

/** Empty numeric value: the validation error list appears below the row. */
export const WithValidationError: Story = {
	args: { predicate: pred({ field: 'rating', op: '>=', value: 4 }) },
	render: (args) => {
		args.predicate.value = '';
		return { Component: RulePredicate, props: args };
	}
};

/** No onRemove: the remove button is not rendered (root predicate). */
export const NotRemovable: Story = {
	args: { predicate: pred({ field: 'energy', op: '>', value: 6 }) },
	render: (args) => ({ Component: RulePredicate, props: { predicate: args.predicate } })
};
