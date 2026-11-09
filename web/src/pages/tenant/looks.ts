/** Matching screen: each state in words and an icon, the colour only repeats them. */
import Ban from '@lucide/svelte/icons/ban';
import CircleQuestionMark from '@lucide/svelte/icons/circle-question-mark';
import Link from '@lucide/svelte/icons/link';
import Lock from '@lucide/svelte/icons/lock';
import MailX from '@lucide/svelte/icons/mail-x';
import Minus from '@lucide/svelte/icons/minus';
import CircleArrowRight from '@lucide/svelte/icons/circle-arrow-right';
import Split from '@lucide/svelte/icons/split';
import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
import UserX from '@lucide/svelte/icons/user-x';
import X from '@lucide/svelte/icons/x';
import { m } from '#lib/paraglide/messages.js';
import type { CoverageState, MatchState } from '../../lib/api/types.ts';
import type { CoverageKind } from '../../lib/logic/tenantmatch.ts';
import type { StatusLook } from '../../lib/status.ts';

export function matchLook(state: MatchState, server: string): StatusLook {
	switch (state) {
		case 'ready':
			return { tone: 'info', icon: CircleArrowRight, label: m.tenant_state_ready() };
		case 'linked':
			return { tone: 'signal', icon: Link, label: m.tenant_state_linked() };
		case 'no_target':
			return { tone: 'advisory', icon: UserX, label: m.tenant_state_no_target({ server }) };
		case 'no_source':
			return { tone: 'ghost', icon: MailX, label: m.tenant_state_no_source() };
		case 'other_source':
			return { tone: 'ghost', icon: Split, label: m.tenant_state_other_source() };
		case 'conflict':
			return { tone: 'warning', icon: TriangleAlert, label: m.tenant_state_conflict() };
		default:
			return { tone: 'ghost', icon: CircleQuestionMark, label: String(state) };
	}
}

/** Chip labels: short, without the server name the header already shows. */
export function chipLabel(state: MatchState | 'all'): string {
	switch (state) {
		case 'all':
			return m.tenant_chip_all();
		case 'ready':
			return m.tenant_state_ready();
		case 'linked':
			return m.tenant_state_linked();
		case 'no_target':
			return m.tenant_chip_no_target();
		case 'no_source':
			return m.tenant_chip_no_source();
		case 'other_source':
			return m.tenant_chip_other_source();
		default:
			return m.tenant_chip_conflict();
	}
}

export function kindName(kind: CoverageKind): string {
	switch (kind) {
		case 'mail':
			return m.tenant_kind_mail();
		case 'calendar':
			return m.tenant_kind_calendar();
		case 'contacts':
			return m.tenant_kind_contacts();
		default:
			return m.tenant_kind_tasks();
	}
}

export interface CoverageLook {
	/** Text colour of the kind icon: green goes, grey does not, red is a problem to fix. */
	tone: string;
	/** Small mark on the icon for every state but "on": the state reads without colour. */
	mark: typeof Ban | null;
	label: string;
}

export function coverageLook(state: CoverageState): CoverageLook {
	switch (state) {
		case 'on':
			return { tone: 'text-signal-ink', mark: null, label: m.tenant_cov_on() };
		case 'off':
			return { tone: 'text-ink-3', mark: Minus, label: m.tenant_cov_off() };
		case 'no_dav':
			return { tone: 'text-warning-ink', mark: Ban, label: m.tenant_cov_no_dav() };
		case 'failed':
			return { tone: 'text-warning-ink', mark: X, label: m.tenant_cov_failed() };
		case 'no_permission':
			return { tone: 'text-ink-3', mark: Lock, label: m.tenant_cov_no_permission() };
		default:
			return { tone: 'text-ink-3', mark: CircleQuestionMark, label: m.tenant_cov_unknown() };
	}
}

export const COVERAGE_STATES: readonly CoverageState[] = ['on', 'off', 'no_dav', 'failed', 'unknown', 'no_permission'];
