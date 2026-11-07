import { SETTINGS_CATALOG } from '$lib/settings/catalog';
import { defineCustomiseScreens } from '$lib/settings/customise-screens';

export const _ = defineCustomiseScreens(SETTINGS_CATALOG, [
	{
		id: 'bogus',
		label: 'Bogus',
		groups: [{ id: 'g', label: 'G', rows: [{ kind: 'setting', id: 'no_such_setting' }] }]
	}
]);
