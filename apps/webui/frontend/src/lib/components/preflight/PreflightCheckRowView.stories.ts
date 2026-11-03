import type { Meta, StoryObj } from '@storybook/sveltekit';
import type { ComponentProps } from 'svelte';
import { fn } from 'storybook/test';

import PreflightCheckRowView from './PreflightCheckRowView.svelte';

type Check = ComponentProps<typeof PreflightCheckRowView>['check'];

const check = (over: Partial<Check> = {}): Check => ({
	id: 'state-db',
	status: 'pass',
	label: 'State database',
	detail: '(private path) opened read-write.',
	severity: 'blocking',
	explainer: 'Where Open DJ keeps your library, cues and analysis.',
	remediation: null,
	...over
});

/**
 * One row of the boot gate (PREFLIGHT-01/02): a severity light, the label,
 * detail, explainer and remediation. The container decides whether the row
 * offers Run setup and owns the click; `isRunSetupBlocked` is read at
 * render. Wrapped rows are <li>, shown here on their own.
 */
const meta = {
	title: 'Components/Preflight/PreflightCheckRowView',
	component: PreflightCheckRowView,
	tags: ['autodocs'],
	decorators: [
		(story) => {
			// The light colors are declared by PreflightScreen, the row's parent
			// in the app; mirror its dark values so the lights show here too.
			const root = typeof document !== 'undefined' ? document.getElementById('storybook-root') : null;
			if (root) {
				root.style.setProperty('--preflight-ok', '#4ecb8c');
				root.style.setProperty('--preflight-warn', '#e8a33d');
			}
			return story();
		}
	],
	args: {
		check: check(),
		offersRunSetup: false,
		runSetupLabel: 'Run setup',
		setupBusy: false,
		setupError: null,
		isRunSetupBlocked: () => false,
		onRunSetup: fn()
	}
} satisfies Meta<typeof PreflightCheckRowView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Passed: green light, collapsed. */
export const Pass: Story = {};

/** Blocking failure: red light, with a System Settings deep link from the remediation. */
export const BlockingFail: Story = {
	args: {
		check: check({
			id: 'audio-access',
			status: 'fail',
			label: 'Audio folder access',
			detail: 'Reading a sample track was refused by macOS.',
			remediation:
				'Grant Full Disk Access: x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles'
		})
	}
};

/** Advisory failure: orange, the app runs anyway. */
export const AdvisoryFail: Story = {
	args: {
		check: check({
			id: 'output-device',
			status: 'fail',
			severity: 'advisory',
			label: 'Audio output',
			detail: 'No headphone cue output found.',
			remediation: 'Plug in a second output to pre-listen.'
		})
	}
};

/** Pending with an empty library: offers Run setup. */
export const RunSetupOffered: Story = {
	args: {
		check: check({
			id: 'library-attached',
			status: 'pending',
			label: 'Library',
			detail: 'Setup was dismissed with an empty library.'
		}),
		offersRunSetup: true
	}
};

/** Run setup clicked and still opening. */
export const RunSetupBusy: Story = {
	args: { ...RunSetupOffered.args, setupBusy: true }
};

/** Run setup refused with a reason. */
export const RunSetupError: Story = {
	args: { ...RunSetupOffered.args, setupError: 'The engine is not reachable yet; try again in a moment.' }
};

/** The admin page's form of a row: raw label plus the status word. */
export const AdminMode: Story = {
	args: {
		mode: 'admin',
		check: check({ status: 'fail', user_label: 'Your library', label: 'state-db writable' })
	}
};
