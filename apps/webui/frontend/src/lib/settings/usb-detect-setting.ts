import type { SettingDef } from './catalog-types';

/** USB-328: toast when the tracker sees a new stick mount. */
export const USB_TOAST_ENABLED_SETTING: SettingDef = {
	id: 'usb_toast_enabled',
	label: 'USB detect toast',
	group: 'library',
	keywords: ['usb', 'stick', 'detect', 'toast', 'mount', 'plug'],
	title: 'Show a toast when a USB stick is newly detected',
	detail:
		'On by default. Dismissible info toast (60 s by default) names the stick. Turning it off leaves the bottom-bar pulse and USB panel entry.',
	implemented: true,
	control: { kind: 'boolean' }
};

/** USB-328: optional focus steal on detect. */
export const USB_AUTO_OPEN_PANEL_SETTING: SettingDef = {
	id: 'usb_auto_open_panel',
	label: 'USB auto-open panel',
	group: 'library',
	keywords: ['usb', 'stick', 'detect', 'panel', 'modal', 'focus'],
	title: 'Open the USB panel automatically when a stick is detected',
	detail:
		'Off by default so mixing is never interrupted. When off, use the pulsing bottom-bar entry, the library USB control, or the toast action to open the panel.',
	implemented: true,
	control: { kind: 'boolean' }
};
