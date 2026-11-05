<script lang="ts">
	import {
		clampSqlLimit,
		fetchSqlQuery,
		formatSqlCell,
		sendPlaygroundRequest,
		type PlaygroundHttpResult,
		type PlaygroundMethod,
		type SqlQueryResult
	} from './playground-api';

	let method = $state<PlaygroundMethod>('GET');
	let apiPath = $state('');
	let apiBody = $state('');
	let apiBusy = $state(false);
	let apiError = $state<string | null>(null);
	let apiResult = $state<PlaygroundHttpResult | null>(null);

	let sqlText = $state('');
	let sqlLimit = $state(200);
	let sqlBusy = $state(false);
	let sqlError = $state<string | null>(null);
	let sqlResult = $state<SqlQueryResult | null>(null);

	async function onSendApi(): Promise<void> {
		if (apiBusy) return;
		apiBusy = true;
		apiError = null;
		apiResult = null;
		try {
			apiResult = await sendPlaygroundRequest({
				method,
				path: apiPath,
				bodyText: apiBody
			});
		} catch (error) {
			apiError = error instanceof Error ? error.message : String(error);
		} finally {
			apiBusy = false;
		}
	}

	async function onRunSql(): Promise<void> {
		if (sqlBusy) return;
		sqlBusy = true;
		sqlError = null;
		sqlResult = null;
		try {
			sqlResult = await fetchSqlQuery(sqlText, clampSqlLimit(sqlLimit));
		} catch (error) {
			sqlError = error instanceof Error ? error.message : String(error);
		} finally {
			sqlBusy = false;
		}
	}
</script>

<div class="playground-panel">
	<section class="panel">
		<h3>API console</h3>
		<p class="sub">
			Send GET/POST/PUT/PATCH/DELETE to a same-origin <code>/api/v1/*</code> path. The raw status, headers,
			and body come from the live daemon.
		</p>
		<div class="form-row">
			<label>
				Method
				<select bind:value={method} disabled={apiBusy}>
					<option value="GET">GET</option>
					<option value="POST">POST</option>
					<option value="PUT">PUT</option>
					<option value="PATCH">PATCH</option>
					<option value="DELETE">DELETE</option>
				</select>
			</label>
			<label class="grow">
				Path
				<input type="text" bind:value={apiPath} placeholder="/api/v1/health" disabled={apiBusy} />
			</label>
			<button type="button" onclick={() => void onSendApi()} disabled={apiBusy}>
				{apiBusy ? 'Sending...' : 'Send'}
			</button>
		</div>
		<label>
			Body (JSON; GET ignores this)
			<textarea bind:value={apiBody} placeholder={'{}'} rows="4" disabled={apiBusy}></textarea>
		</label>
		{#if apiError}
			<div class="fatal">{apiError}</div>
		{:else if apiResult}
			<div class="result">
				<p><strong>HTTP {apiResult.status}</strong></p>
				{#if Object.keys(apiResult.headers).length > 0}
					<p class="sub">Headers: {JSON.stringify(apiResult.headers)}</p>
				{/if}
				<pre>{apiResult.body || '(empty body)'}</pre>
			</div>
		{/if}
	</section>

	<section class="panel">
		<h3>SQL console</h3>
		<p class="sub">
			Read-only against the daemon <code>state.db</code>. Write statements are refused.
		</p>
		<label>
			SQL
			<textarea
				bind:value={sqlText}
				placeholder="SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1"
				rows="5"
				disabled={sqlBusy}></textarea>
		</label>
		<div class="form-row">
			<label>
				Limit
				<input type="number" bind:value={sqlLimit} min="1" max="500" disabled={sqlBusy} />
			</label>
			<button type="button" onclick={() => void onRunSql()} disabled={sqlBusy}>
				{sqlBusy ? 'Running...' : 'Run'}
			</button>
		</div>
		{#if sqlError}
			<div class="fatal">{sqlError}</div>
		{:else if sqlResult}
			{#if sqlResult.truncated}
				<p class="sub">showing {sqlResult.row_count} rows (truncated)</p>
			{/if}
			<table>
				<thead>
					<tr>
						{#each sqlResult.columns as column (column)}
							<th>{column}</th>
						{/each}
					</tr>
				</thead>
				<tbody>
					{#each sqlResult.rows as row, rowIndex (rowIndex)}
						<tr>
							{#each row as cell, cellIndex (`${rowIndex}-${cellIndex}`)}
								<td>{formatSqlCell(cell)}</td>
							{/each}
						</tr>
					{/each}
				</tbody>
			</table>
			{#if sqlResult.row_count === 0}
				<p class="sub">0 rows</p>
			{/if}
		{/if}
	</section>
</div>

<style>
	@import './PlaygroundPanel.css';
</style>
