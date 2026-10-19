import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './EditSuiteModal.contract';
import { DEMO_BODY_TEXT } from './EditSuiteModal.contract';
import EditSuiteModal from './EditSuiteModal';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
// The child matches the Svelte story's snippet.
const meta = {
	title: 'Contracts/Root/EditSuiteModal',
	component: EditSuiteModal,
	args: { children: <p className="es-demo">{DEMO_BODY_TEXT}</p> }
} satisfies Meta<typeof EditSuiteModal>;
export default meta;
type Story = StoryObj<typeof meta>;

export const ClickToClose: Story = contracts.ClickToClose;
export const EscapeToClose: Story = contracts.EscapeToClose;
