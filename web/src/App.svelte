<script lang="ts">
	import { onMount } from 'svelte';
	import { get, setUnauthorizedHandler } from './lib/api/client.ts';
	import { i18n } from './lib/i18n.svelte.ts';
	import { prefs } from './lib/prefs.svelte.ts';
	import { data } from './lib/stores/data.svelte.ts';
	import Skeleton from './lib/ui/Skeleton.svelte';
	import Toaster from './lib/ui/Toaster.svelte';
	import Login from './pages/Login.svelte';
	import Shell from './pages/Shell.svelte';

	let auth = $state<'checking' | 'in' | 'out'>('checking');
	let expired = $state(false);

	async function enter() {
		auth = 'in';
		expired = false;
		await data.start();
	}

	function leave(sessionExpired: boolean) {
		data.stop();
		auth = 'out';
		expired = sessionExpired;
	}

	setUnauthorizedHandler(() => {
		if (auth === 'in') leave(true);
	});

	onMount(() => {
		prefs.apply();
		get('/me', { noAuthRedirect: true }).then(enter, () => leave(false));
	});
</script>

{#key i18n.locale}
	{#if auth === 'checking'}
		<div class="p-6"><Skeleton class="h-10" /></div>
	{:else if auth === 'out'}
		<Login {expired} onlogin={enter} />
	{:else}
		<Shell onlogout={() => leave(false)} />
	{/if}
	<Toaster />
{/key}
