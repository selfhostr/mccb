<script lang="ts">
	/**
	 * The one way in to add a server, whatever it is: first what kind, then its form. An IMAP server
	 * and a Microsoft 365 tenant are stored differently, which is no reason to add them in two places.
	 */
	import type { Component } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import type { Server, SourceConnection } from '../../lib/api/types.ts';
	import { kindIcon, PROVIDERS, type Provider } from '../../lib/logic/providers.ts';
	import type { ServerPreset as Preset } from '../../lib/logic/serverKinds.ts';
	import { connectors } from '../../lib/stores/connectors.svelte.ts';
	import Dialog from '../../lib/ui/Dialog.svelte';
	import KindIcon from '../../lib/ui/KindIcon.svelte';
	import ServerDialog from './ServerDialog.svelte';
	import SourceDialog from './SourceDialog.svelte';

	interface Props {
		onclose: () => void;
		onsaved?: (saved: Server) => void;
		onconnection?: (saved: SourceConnection) => void;
	}
	let { onclose, onsaved, onconnection }: Props = $props();

	interface Choice {
		id: string;
		name: string;
		hint: string;
		icon: Component<{ size?: number }>;
		/** The initial of a named service, shown instead of the icon. */
		mark?: string;
		/** An IMAP server, filled from the provider. */
		provider?: Provider;
		/** Or a connection of this kind. */
		kind?: string;
		preset?: Preset;
	}

	const provider = (id: string) => PROVIDERS.find((p) => p.id === id)!;
	const imap = (id: string, hint: string, name?: string): Choice => ({ id, name: name ?? provider(id).name, hint, icon: provider(id).icon, mark: provider(id).mark, provider: provider(id) });
	const MARKS: Record<string, string> = { m365: 'M', outlook: 'O', workspace: 'G' };
	const connection = (id: string, kind: string, name: string, hint: string, preset: Preset = {}): Choice => ({ id, name, hint, icon: kindIcon(kind), mark: MARKS[id], kind, preset: { title: name, ...preset } });

	const OUTLOOK = {
		host: 'outlook.office365.com',
		port: 993,
		security: 'ssl',
		verify_tls: true,
		token_url: 'https://login.microsoftonline.com/consumers/oauth2/v2.0/token',
		scope: 'https://outlook.office.com/IMAP.AccessAsUser.All offline_access'
	};

	// A kind the server does not know (an older image) is not offered.
	const known = (c: Choice) => !c.kind || connectors.list.some((k) => k.kind === c.kind);
	const GROUPS = $derived(
		[
			{
				id: 'services',
				title: m.servers_pick_services(),
				choices: [
					imap('gmail', m.servers_type_gmail_hint()),
					connection('m365', 'm365_graph', 'Microsoft 365', m.servers_type_m365_hint()),
					connection('outlook', 'imap_oauth2', m.servers_type_outlook(), m.servers_type_outlook_hint(), {
						name: 'Outlook.com',
						config: OUTLOOK,
						note: m.servers_note_outlook(),
						link: { label: m.servers_link_outlook_app(), href: 'https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade' }
					}),
					connection('workspace', 'google', 'Google Workspace', m.servers_type_workspace_hint(), { name: 'Google Workspace', config: { auth: 'service_account' }, note: m.servers_note_workspace() }),
					imap('icloud', m.servers_type_dav_hint()),
					imap('fastmail', m.servers_type_dav_hint()),
					imap('infomaniak', m.servers_type_infomaniak_hint())
				]
			},
			{
				id: 'own',
				title: m.servers_pick_own(),
				choices: [
					imap('imap', m.servers_type_imap_hint(), m.servers_type_imap()),
					connection('exchange', 'exchange_ews', m.servers_type_exchange(), m.servers_type_exchange_hint(), { note: m.servers_note_exchange() }),
					connection('jmap', 'jmap', m.servers_type_jmap(), m.servers_type_jmap_hint(), { note: m.servers_note_jmap() })
				]
			}
		].map((g) => ({ ...g, choices: g.choices.filter(known) }))
	);

	let picked = $state.raw<Choice | null>(null);
	void connectors.load();
</script>

{#if picked?.provider}
	<ServerDialog provider={picked.provider} {onclose} onback={() => (picked = null)} {onsaved} />
{:else if picked?.kind}
	<SourceDialog kind={picked.kind} preset={picked.preset} {onclose} onback={() => (picked = null)} onsaved={onconnection} />
{:else}
	<Dialog title={m.settings_server_add_title()} {onclose}>
		<div class="flex flex-col gap-4" data-testid="server-kinds">
			{#each GROUPS as group (group.id)}
				<section>
					<h3 class="m-0 mb-2 text-xs font-medium text-ink-2">{group.title}</h3>
					<ul class="m-0 flex list-none flex-col gap-1.5 p-0">
						{#each group.choices as choice (choice.id)}
							<li>
								<button
									type="button"
									class="flex h-full min-h-14 w-full items-center gap-3 rounded-btn border border-line bg-surface px-3 py-2 text-left transition-colors duration-200 hover:border-line-strong hover:bg-ghost"
									data-kind={choice.id}
									onclick={() => (picked = choice)}
								>
									<KindIcon icon={choice.icon} mark={choice.mark} />
									<span class="min-w-0">
										<span class="block font-medium">{choice.name}</span>
										<span class="block text-xs text-ink-2">{choice.hint}</span>
									</span>
								</button>
							</li>
						{/each}
					</ul>
				</section>
			{/each}
		</div>
	</Dialog>
{/if}
