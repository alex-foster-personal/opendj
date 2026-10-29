import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let usb;

before(async () => {
	usb = await loadTypeScriptModule('src/lib/rb/usb-tracker.svelte.ts');
});

describe('usb tracker helpers (#328)', () => {
	it('keeps the fake-volume simulator out of the shipped UI and client API', () => {
		const panel = readFileSync(
			new URL('../../src/lib/components/rb/UsbPanel.svelte', import.meta.url),
			'utf8'
		);
		const tracker = readFileSync(
			new URL('../../src/lib/rb/usb-tracker.svelte.ts', import.meta.url),
			'utf8'
		);

		assert.doesNotMatch(panel, /Simulate USB|simulateUsbVolume/);
		assert.doesNotMatch(tracker, /simulateUsbVolume|api\.POST/);
	});

	it('keeps non-music present volumes out of the active list (no double list)', () => {
		const music = {
			id: 'vol:1',
			name: 'STICK',
			first_seen: 1,
			last_seen: 2,
			present: true,
			forgotten: false,
			is_music: true,
			kind: 'music',
			role: 'usb_stick'
		};
		const backup = {
			id: 'vol:2',
			name: 'MaintainerBackup',
			first_seen: 1,
			last_seen: 2,
			present: true,
			forgotten: false,
			is_music: false,
			kind: 'unknown',
			role: 'mounted_drive',
			hide_reason: 'not-usb(mounted drive - USB)'
		};
		assert.equal(usb.isActiveUsbRow(music), true);
		assert.equal(usb.isActiveUsbRow(backup), false);
		assert.equal(usb.isFoldedUsbRow(backup), true);
		assert.equal(usb.isFoldedUsbRow(music), false);
	});

	it('formats forget reason dates clearly', () => {
		// 8 Aug 2026 local - construct via UTC noon to avoid TZ edge flakiness.
		const ms = Date.UTC(2026, 7, 8, 12, 0, 0);
		const label = usb.formatShortDate(ms);
		assert.match(label, /8th Aug '26/);
		assert.equal(usb.dayOrdinal(1), 'st');
		assert.equal(usb.dayOrdinal(2), 'nd');
		assert.equal(usb.dayOrdinal(3), 'rd');
		assert.equal(usb.dayOrdinal(11), 'th');
		assert.equal(usb.dayOrdinal(22), 'nd');
	});

	it('prefers hide_reason then forget stamp for folded tags', () => {
		assert.equal(
			usb.usbRowReasonTag({
				id: 'a',
				name: 'MaintainerBackup',
				first_seen: 1,
				last_seen: 1,
				is_music: false,
				hide_reason: 'not-usb(mounted drive - USB)'
			}),
			'not-usb(mounted drive - USB)'
		);
		const forgottenAt = Date.UTC(2026, 7, 8, 12, 0, 0);
		assert.equal(
			usb.usbRowReasonTag({
				id: 'b',
				name: 'OldStick',
				first_seen: 1,
				last_seen: 1,
				forgotten: true,
				forgotten_at: forgottenAt
			}),
			`user clicked forget ${usb.formatShortDate(forgottenAt)}`
		);
	});

	it('labels mounted drives without a mysterious slash', () => {
		assert.equal(
			usb.usbRowKindLabel({
				id: 'c',
				name: 'MaintainerBackup',
				first_seen: 1,
				last_seen: 1,
				role: 'mounted_drive',
				is_music: false,
				kind: 'unknown'
			}),
			'mounted drive · non-music'
		);
		assert.equal(
			usb.usbRowKindLabel({
				id: 'd',
				name: 'GitHub Copilot',
				first_seen: 1,
				last_seen: 1,
				role: 'disk_image',
				is_music: false,
				kind: 'unknown'
			}),
			'disk image · non-music'
		);
	});

	it('says when one RBX drive failed to read, and that it is not the other library', () => {
		const report = usb.usbFormatReport(
			[
				{
					id: 'vol:ssk',
					name: 'SSK',
					present: true,
					kind: 'rekordbox',
					role: 'usb_stick',
					is_music: true
				}
			],
			{
				'vol:ssk': {
					state: 'failed',
					code: 'USB_LIBRARY_READ_FAILED',
					detail: 'export.pdb could not be parsed'
				}
			}
	,
			true
		);
		assert.equal(report.drives, 'One drive only: SSK.');
		assert.equal(report.libraries, 'Only rekordbox (RBX). Not the other library (djay).');
		assert.equal(report.sticks.length, 1);
		assert.match(report.sticks[0].version, /USB read failed \(export\.pdb could not be parsed\)/);
		assert.match(report.sticks[0].version, /rekordbox \(RBX\) format/);
		assert.match(report.sticks[0].version, /Device Library Plus/);
	});

	it('names a successful RBX read as Device Library and keeps a second drive visible', () => {
		const report = usb.usbFormatReport(
			[
				{ id: 'vol:ssk', name: 'SSK', present: true, kind: 'rekordbox' },
				{ id: 'vol:other', name: 'BACKUP', present: true, kind: 'djay' },
				{ id: 'vol:gone', name: 'OLD', present: false, kind: 'rekordbox' }
			],
			{ 'vol:ssk': { state: 'ok', trackCount: 12 } }
	,
			true
		);
		assert.equal(report.drives, 'Not one drive only. 2 drives: SSK, BACKUP.');
		assert.equal(
			report.libraries,
			'Both rekordbox (RBX) and djay are mounted. Not only one or the other.'
		);
		assert.match(report.sticks[0].version, /12 tracks/);
		assert.match(report.sticks[0].version, /export\.pdb, RBX/);
		assert.match(report.sticks[1].version, /Classified as djay, the other library/);
	});

	// Sol P1 4399554460, PR #6223: before this session's first scan the present
	// flags are last session's, so no drive is named as mounted.
	it('names no mounted drive before the first scan, and does once one lands', () => {
		const persisted = [{ id: 'vol:ssk', name: 'SSK', present: true, kind: 'rekordbox' }];
		const before = usb.usbFormatReport(persisted, {}, false);
		assert.equal(before.drives, 'Checking drives: none scanned yet this session.');
		assert.equal(before.libraries, 'Not known until the first scan.');
		assert.deepEqual(before.sticks, []);
		// Control: the same volumes after a scan are reported as mounted.
		const after = usb.usbFormatReport(persisted, {}, true);
		assert.equal(after.drives, 'One drive only: SSK.');
		assert.equal(after.sticks.length, 1);
	});

	it('the panel passes whether this session has scanned yet', () => {
		const panel = readFileSync(
			new URL('../../src/lib/components/rb/UsbPanel.svelte', import.meta.url),
			'utf8'
		);
		assert.match(panel, /usbFormatReport\(usbTracker\.volumes, usbTracker\.readNotes, usbTracker\.scannedAt !== null\)/);
	});

	it('a plan view that failed to load offers a Retry that re-arms the loader (Sol P2, PR #6223)', () => {
		const panel = readFileSync(
			new URL('../../src/lib/components/rb/UsbPanel.svelte', import.meta.url),
			'utf8'
		);
		assert.match(panel, /if \(!usbImportView\.open \|\| PlanView !== null \|\| planFailed\) return;/);
		assert.match(panel, /data-testid="usb-import-plan-retry" onclick=\{\(\) => \(planFailed = false\)\}>Retry</);
	});

	it('keeps the USB modal copy for version, one-drive, and the coming-soon bench', () => {
		const panel = readFileSync(
			new URL('../../src/lib/components/rb/UsbPanel.svelte', import.meta.url),
			'utf8'
		);
		assert.match(panel, /data-testid="usb-format-report"/);
		assert.match(panel, /data-testid="usb-compat-soon"/);
		assert.match(panel, /\[Coming soon\]/);
		assert.match(panel, /read\/write speed benchmarking/);
		assert.match(panel, /old CDJ/);
		assert.match(panel, /Fixes to try/);
	});
});
