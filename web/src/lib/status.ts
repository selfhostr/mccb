import Ban from '@lucide/svelte/icons/ban';
import CircleAlert from '@lucide/svelte/icons/circle-alert';
import CircleCheck from '@lucide/svelte/icons/circle-check';
import CircleDashed from '@lucide/svelte/icons/circle-dashed';
import CircleSlash from '@lucide/svelte/icons/circle-slash';
import Clock from '@lucide/svelte/icons/clock';
import CopyX from '@lucide/svelte/icons/copy-x';
import Download from '@lucide/svelte/icons/download';
import FileX from '@lucide/svelte/icons/file-x';
import FlaskConical from '@lucide/svelte/icons/flask-conical';
import Hourglass from '@lucide/svelte/icons/hourglass';
import Circle from '@lucide/svelte/icons/circle';
import Pause from '@lucide/svelte/icons/pause';
import Play from '@lucide/svelte/icons/play';
import ScanSearch from '@lucide/svelte/icons/scan-search';
import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
import Undo2 from '@lucide/svelte/icons/undo-2';
import Upload from '@lucide/svelte/icons/upload';
import Wrench from '@lucide/svelte/icons/wrench';
import { m } from '#lib/paraglide/messages.js';
import type { AccountStatus, DedupBox, Job, JobStatus, Pst, RepairBox } from './api/types.ts';
import type { TestPhase } from './logic/testrun.ts';
import type { UploadStatus } from './upload/queue.ts';
import type { Tone } from './ui/Plate.svelte';

export interface StatusLook {
	tone: Tone;
	icon: typeof CircleCheck;
	label: string;
}

export function jobLook(status: JobStatus): StatusLook {
	switch (status) {
		case 'queued':
			return { tone: 'ghost', icon: Clock, label: m.status_job_queued() };
		case 'running':
			return { tone: 'info', icon: Play, label: m.status_job_running() };
		case 'paused':
			return { tone: 'advisory', icon: Pause, label: m.status_job_paused() };
		case 'stopping':
			return { tone: 'advisory', icon: Hourglass, label: m.status_job_stopping() };
		case 'stopped':
			return { tone: 'ghost', icon: Ban, label: m.status_job_stopped() };
		case 'done':
			return { tone: 'signal', icon: CircleCheck, label: m.status_job_done() };
		case 'done_with_errors':
			return { tone: 'advisory', icon: CircleAlert, label: m.status_job_done_errors() };
		case 'failed':
			return { tone: 'warning', icon: TriangleAlert, label: m.status_job_failed() };
		default:
			return { tone: 'ghost', icon: CircleDashed, label: String(status) };
	}
}

/** A job paused by the allowed hours is waiting, not stopped and not failed. */
export function jobLookOf(job: Pick<Job, 'status' | 'pause_reason'>): StatusLook {
	if (job.status === 'paused' && job.pause_reason === 'window') return { tone: 'info', icon: Clock, label: m.status_job_waiting_hours() };
	return jobLook(job.status);
}

/** A running connection test, in words. */
export function testLook(phase: TestPhase): StatusLook {
	return phase === 'running' ? { tone: 'info', icon: FlaskConical, label: m.migrations_test_running() } : { tone: 'info', icon: Clock, label: m.migrations_test_waiting() };
}

/** A mailbox without a job: what stands between it and an import. */
export function accountLook(status: AccountStatus, hasSource: boolean): StatusLook {
	if (status === 'auth_failed') return { tone: 'warning', icon: TriangleAlert, label: m.migrations_row_login_refused() };
	if (status === 'error') return { tone: 'warning', icon: TriangleAlert, label: m.migrations_test_status_failed() };
	if (!hasSource) return { tone: 'ghost', icon: FileX, label: m.migrations_filter_no_pst() };
	if (status === 'new') return { tone: 'ghost', icon: CircleDashed, label: m.migrations_test_never() };
	return { tone: 'ghost', icon: Circle, label: m.migrations_row_ready() };
}

export function pstLook(status: Pst['status']): StatusLook {
	switch (status) {
		case 'uploading':
			return { tone: 'advisory', icon: Upload, label: m.status_pst_uploading() };
		case 'validating':
			return { tone: 'info', icon: ScanSearch, label: m.status_pst_validating() };
		case 'valid':
			return { tone: 'signal', icon: CircleCheck, label: m.status_pst_valid() };
		case 'invalid':
			return { tone: 'warning', icon: TriangleAlert, label: m.status_pst_invalid() };
		case 'fetching':
			return { tone: 'info', icon: Download, label: m.status_pst_fetching() };
		case 'cancelling':
			return { tone: 'advisory', icon: Hourglass, label: m.status_pst_cancelling() };
		case 'fetch_failed':
			return { tone: 'warning', icon: TriangleAlert, label: m.status_pst_fetch_failed() };
		default:
			return { tone: 'ghost', icon: CircleDashed, label: String(status) };
	}
}

export function uploadLook(status: UploadStatus): StatusLook {
	switch (status) {
		case 'needs_file':
			return { tone: 'advisory', icon: CircleSlash, label: m.status_upload_needs_file() };
		case 'queued':
			return { tone: 'ghost', icon: Clock, label: m.status_upload_queued() };
		case 'starting':
		case 'uploading':
			return { tone: 'info', icon: Upload, label: m.status_upload_uploading() };
		case 'completing':
			return { tone: 'info', icon: Hourglass, label: m.status_upload_completing() };
		case 'paused':
			return { tone: 'advisory', icon: Pause, label: m.status_upload_paused() };
		case 'error':
			return { tone: 'warning', icon: TriangleAlert, label: m.status_upload_error() };
		default:
			return { tone: 'signal', icon: CircleCheck, label: m.status_upload_done() };
	}
}

/** A repair mailbox: what runs now, else what the last run found. */
export function repairLook(box: Pick<RepairBox, 'active_run' | 'last_scan'>): StatusLook {
	const run = box.active_run;
	if (run) {
		if (run.status === 'queued') return { tone: 'ghost', icon: Clock, label: m.repair_state_queued() };
		if (run.status === 'stopping') return { tone: 'advisory', icon: Hourglass, label: m.status_job_stopping() };
		if (run.mode === 'scan') return { tone: 'info', icon: ScanSearch, label: m.repair_state_scanning() };
		if (run.mode === 'repair') return { tone: 'info', icon: Wrench, label: m.repair_state_repairing() };
		return { tone: 'info', icon: Undo2, label: m.repair_state_restoring() };
	}
	const last = box.last_scan;
	if (!last) return { tone: 'ghost', icon: CircleDashed, label: m.repair_state_never() };
	if (last.status === 'failed') return { tone: 'warning', icon: TriangleAlert, label: m.status_job_failed() };
	if (last.status === 'stopped') return { tone: 'ghost', icon: Ban, label: m.status_job_stopped() };
	if (last.mode === 'restore') return { tone: 'ghost', icon: Undo2, label: m.repair_state_restored() };
	if (last.mode === 'repair') {
		return last.status === 'done_with_errors' ? { tone: 'advisory', icon: CircleAlert, label: m.repair_state_repaired_errors() } : { tone: 'signal', icon: CircleCheck, label: m.repair_state_repaired() };
	}
	return last.progress.affected ? { tone: 'advisory', icon: TriangleAlert, label: m.repair_state_damaged() } : { tone: 'signal', icon: CircleCheck, label: m.repair_state_intact() };
}

/** A mailbox of the duplicates tab: what runs now, else what its last run found or did. */
export function dedupLook(box: Pick<DedupBox, 'active_run' | 'last_run' | 'last_scan'>): StatusLook {
	const run = box.active_run;
	if (run) {
		if (run.status === 'queued') return { tone: 'ghost', icon: Clock, label: m.dedup_state_queued() };
		if (run.status === 'stopping') return { tone: 'advisory', icon: Hourglass, label: m.status_job_stopping() };
		if (run.mode === 'scan') return { tone: 'info', icon: ScanSearch, label: m.dedup_state_scanning() };
		return { tone: 'info', icon: CopyX, label: m.dedup_state_removing() };
	}
	const last = box.last_run ?? box.last_scan;
	if (!last) return { tone: 'ghost', icon: CircleDashed, label: m.dedup_state_never() };
	if (last.status === 'failed') return { tone: 'warning', icon: TriangleAlert, label: m.status_job_failed() };
	if (last.status === 'stopped') return { tone: 'ghost', icon: Ban, label: m.status_job_stopped() };
	if (last.mode === 'dedup') {
		return last.status === 'done_with_errors' ? { tone: 'advisory', icon: CircleAlert, label: m.dedup_state_removed_errors() } : { tone: 'signal', icon: CircleCheck, label: m.dedup_state_removed() };
	}
	return last.progress.duplicates ? { tone: 'advisory', icon: TriangleAlert, label: m.dedup_state_found() } : { tone: 'signal', icon: CircleCheck, label: m.dedup_state_none() };
}
