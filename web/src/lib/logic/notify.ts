import type { IntakeSettings, NotificationEvent, NotifyChannel } from '../api/types.ts';

type Notifications = IntakeSettings['notifications'];

/** Events as the person thinks of them, in the order they are shown. */
export const EVENT_GROUPS: { id: 'files' | 'migrations' | 'phases' | 'server'; events: NotificationEvent[] }[] = [
	{ id: 'files', events: ['upload_finished', 'analysis_valid', 'analysis_invalid'] },
	{ id: 'migrations', events: ['job_done', 'job_done_with_errors', 'job_failed', 'job_stopped', 'batch_finished'] },
	{ id: 'phases', events: ['phase_mail', 'phase_calendar', 'phase_contacts'] },
	{ id: 'server', events: ['disk_low', 'repeated_crash'] }
];

/** True for the public ntfy.sh service, where a topic name is the only protection. */
export function isPublicNtfy(url: string): boolean {
	try {
		const host = new URL(url.trim() || 'https://ntfy.sh').hostname.toLowerCase();
		return host === 'ntfy.sh' || host.endsWith('.ntfy.sh');
	} catch {
		return /(^|\/\/|\.)ntfy\.sh(\/|:|$)/i.test(url);
	}
}

export interface SecretState {
	value: string;
	clear: boolean;
}

const secretPart = (s: SecretState): string | undefined => (s.value ? s.value : s.clear ? '' : undefined);

export interface NotifySecrets {
	ntfy: SecretState;
	webhook: SecretState;
	email: SecretState;
}

/**
 * Body of PUT /api/intake/settings for the notifications form. Secrets are added only when touched:
 * omitted keeps the stored one, "" clears it.
 */
export function notificationsPatch(draft: Notifications, secrets: NotifySecrets): { notifications: Record<string, unknown> } {
	const { ntfy, webhook, email } = draft;
	const withSecret = (base: Record<string, unknown>, key: string, s: SecretState) => {
		const v = secretPart(s);
		return v === undefined ? base : { ...base, [key]: v };
	};
	return {
		notifications: {
			enabled: draft.enabled,
			mask_addresses: draft.mask_addresses,
			events: { ...draft.events },
			ntfy: withSecret({ enabled: ntfy.enabled, url: ntfy.url, topic: ntfy.topic, priority: Number(ntfy.priority) }, 'token', secrets.ntfy),
			webhook: withSecret({ enabled: webhook.enabled, url: webhook.url }, 'token', secrets.webhook),
			email: withSecret(
				{ enabled: email.enabled, host: email.host, port: Number(email.port), security: email.security, username: email.username, from: email.from, to: email.to },
				'password',
				secrets.email
			)
		}
	};
}

/** Whether the form differs from what is stored. */
export function notificationsDirty(draft: Notifications, stored: Notifications, secrets: NotifySecrets): boolean {
	if (Object.values(secrets).some((s) => s.value || s.clear)) return true;
	const plain = (n: Notifications) =>
		JSON.stringify({
			enabled: n.enabled,
			mask: n.mask_addresses,
			events: Object.keys(n.events).sort().map((k) => [k, n.events[k as NotificationEvent]]),
			ntfy: [n.ntfy.enabled, n.ntfy.url, n.ntfy.topic, Number(n.ntfy.priority)],
			webhook: [n.webhook.enabled, n.webhook.url],
			email: [n.email.enabled, n.email.host, Number(n.email.port), n.email.security, n.email.username, n.email.from, n.email.to]
		});
	return plain(draft) !== plain(stored);
}

export const CHANNEL_ORDER: NotifyChannel[] = ['ntfy', 'webhook', 'email'];

/** Channels enabled in the form, to say what a test will reach. */
export const enabledChannels = (n: Notifications): NotifyChannel[] => CHANNEL_ORDER.filter((c) => n[c].enabled);
