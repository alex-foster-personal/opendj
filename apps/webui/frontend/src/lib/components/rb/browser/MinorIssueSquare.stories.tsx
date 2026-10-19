import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './MinorIssueSquare.contract';
import MinorIssueSquare from './MinorIssueSquare';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Browser/MinorIssueSquare', component: MinorIssueSquare } satisfies Meta<typeof MinorIssueSquare>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Several: Story = contracts.Several;
export const One: Story = contracts.One;
export const None: Story = contracts.None;
