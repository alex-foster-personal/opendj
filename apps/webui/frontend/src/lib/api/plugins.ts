/**
 * The plugin store, read and changed through `apps/engine_core/plugins_api.py`.
 *
 * Every rule lives on the server (labs opt-in, third-party terms notice, permission
 * consent, signed catalog). This module only carries calls; a refusal comes
 * back as a thrown ApiError whose `detail.message` the page shows verbatim,
 * so the page and an agent driving the API read the same sentence.
 *
 * The pure display helpers live in `$lib/plugins/plugin-status.ts`, which has
 * no runtime imports so tests/unit/plugin-store.test.mjs can load it directly.
 */

import { api, unwrap } from './client';
import type { components } from '../api-types';

export type PluginsOut = components['schemas']['PluginsOut'];
export type PluginRow = components['schemas']['PluginRowOut'];
export type ChannelOut = components['schemas']['ChannelOut'];
export type ChangeOut = components['schemas']['ChangeOut'];
export type UiSlot = components['schemas']['UiSlotOut'];
export type SideloadPreview = components['schemas']['SideloadPreviewOut'];

export function getPlugins(refresh = false): Promise<PluginsOut> {
	return unwrap(api.GET('/api/v1/plugins', { params: { query: { refresh } } }));
}

export function setLabs(optIn: boolean): Promise<PluginsOut> {
	return unwrap(api.POST('/api/v1/plugins/labs', { body: { opt_in: optIn } }));
}

export function installPlugin(
	row: PluginRow,
	acknowledgeNotice: boolean
): Promise<ChangeOut> {
	return unwrap(
		api.POST('/api/v1/plugins/{plugin_id}/install', {
			params: { path: { plugin_id: row.plugin_id } },
			body: {
				channel: row.channel,
				accept_permissions: row.permissions,
				acknowledge_notice: acknowledgeNotice,
				// The notice on screen: a catalog refresh that changed it since is refused.
				notice: row.notice,
				// The build on screen: a refresh that swapped it since is refused too.
				archive_sha256: row.available_sha256 ?? ''
			}
		})
	);
}

export function setEnabled(pluginId: string, enabled: boolean): Promise<ChangeOut> {
	const params = { params: { path: { plugin_id: pluginId } } };
	return enabled
		? unwrap(api.POST('/api/v1/plugins/{plugin_id}/enable', params))
		: unwrap(api.POST('/api/v1/plugins/{plugin_id}/disable', params));
}

export function uninstallPlugin(pluginId: string): Promise<ChangeOut> {
	return unwrap(
		api.DELETE('/api/v1/plugins/{plugin_id}', { params: { path: { plugin_id: pluginId } } })
	);
}

/** What a sideload of `path` asks consent for. Reads the manifest only. */
export function previewSideload(path: string): Promise<SideloadPreview> {
	return unwrap(api.POST('/api/v1/plugins/sideload/preview', { body: { path } }));
}

/**
 * The same consent a store install asks for, bound to the build the preview
 * showed: its exact permissions, its terms notice when it has one, and its
 * manifest's sha256. A source rebuilt or replaced since the preview is refused.
 */
export function sideloadPlugin(
	path: string,
	preview: SideloadPreview,
	acknowledgeNotice: boolean
): Promise<ChangeOut> {
	return unwrap(
		api.POST('/api/v1/plugins/sideload', {
			body: {
				path,
				accept_permissions: preview.permissions,
				acknowledge_notice: acknowledgeNotice,
				manifest_sha256: preview.manifest_sha256
			}
		})
	);
}

export function getUiSlots(): Promise<UiSlot[]> {
	return unwrap(api.GET('/api/v1/plugins/ui-slots'));
}

export {
	badges,
	canSideload,
	installConfirmCopy,
	labsShown,
	listingStatusBadge,
	primaryAction,
	screenshotsFor,
	statusLine,
	type PluginAction
} from '../plugins/plugin-status';
