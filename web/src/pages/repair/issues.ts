import { m } from '#lib/paraglide/messages.js';
import type { RepairIssue } from '../../lib/api/types.ts';

/** A kind of damage, in words. */
export function issueLabel(issue: RepairIssue): string {
	switch (issue) {
		case 'replacement_chars':
			return m.repair_issue_replacement_chars();
		case 'mojibake':
			return m.repair_issue_mojibake();
		case 'attachment_names':
			return m.repair_issue_attachment_names();
		case 'detached_images':
			return m.repair_issue_detached_images();
		default:
			return m.repair_issue_unreadable();
	}
}
