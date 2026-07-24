import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let usb;

before(async () => {
	usb = await loadTypeScriptModule('src/lib/rb/usb-tracker.svelte.ts');
});

describe('usb tracker helpers (#328)', () => {
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
});
