import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';

import { contracts } from './EditSuiteModal.contract';
import { DEMO_BODY_TEXT } from './EditSuiteModal.contract';
import EditSuiteModal from './EditSuiteModal.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
// The child is the twin story's <p className="es-demo">, as a snippet.
const meta = {
	title: 'Contracts/Root/EditSuiteModal',
	component: EditSuiteModal,
	args: {
		children: createRawSnippet(() => ({ render: () => `<p class="es-demo">${DEMO_BODY_TEXT}</p>` }))
	}
} satisfies Meta<typeof EditSuiteModal>;
export default meta;
type Story = StoryObj<typeof meta>;

export const ClickToClose: Story = contracts.ClickToClose;
export const EscapeToClose: Story = contracts.EscapeToClose;
