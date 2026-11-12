export async function fetchRequestsState(api) {
	const [sessionRes, listRes] = await Promise.all([
		api('/remote/api/requests/session'),
		api('/remote/api/requests')
	]);
	const session = sessionRes.ok ? await sessionRes.json() : { open: false };
	const listed = listRes.ok ? await listRes.json() : { requests: [] };
	return {
		session,
		requests: Array.isArray(listed.requests) ? listed.requests : [],
		inlineReason: null
	};
}

export async function postRequestAction(api, requestId, action) {
	return api(`/remote/api/requests/${encodeURIComponent(requestId)}:${action}`, {
		method: 'POST',
		body: '{}'
	});
}

export async function putRequestSession(api, body) {
	return api('/remote/api/requests/session', {
		method: 'PUT',
		body: JSON.stringify(body)
	});
}

export async function postLoadCommand(api, deck, stableId) {
	return api('/remote/api/command', {
		method: 'POST',
		body: JSON.stringify({ single: { type: 'load', deck, stable_id: stableId } })
	});
}

export async function loadRequestToDeck(api, row, deck) {
	if (row?.kind === 'track' && row.stable_id) {
		const command = await postLoadCommand(api, deck, row.stable_id);
		if (!command.ok) return command;
	}
	return postRequestAction(api, row.id, 'loaded');
}
