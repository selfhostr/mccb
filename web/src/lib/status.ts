import Ban from '@lucide/svelte/icons/ban';
import CircleAlert from '@lucide/svelte/icons/circle-alert';
import CircleCheck from '@lucide/svelte/icons/circle-check';
import CircleDashed from '@lucide/svelte/icons/circle-dashed';
import CircleSlash from '@lucide/svelte/icons/circle-slash';
import Clock from '@lucide/svelte/icons/clock';
import LoaderCircle from '@lucide/svelte/icons/loader-circle';
import Pause from '@lucide/svelte/icons/pause';
import ScanSearch from '@lucide/svelte/icons/scan-search';
import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
import Upload from '@lucide/svelte/icons/upload';
import { m } from '#lib/paraglide/messages.js';
import type { JobStatus, Pst } from './api/types.ts';
import type { UploadStatus } from './upload/queue.ts';
import type { Tone } from './ui/Plate.svelte';

export interface StatusLook {
	tone: Tone;
	icon: typeof CircleCheck;
	label: string;
	spin?: boolean;
}

export function jobLook(status: JobStatus): StatusLook {
	switch (status) {
		case 'queued':
			return { tone: 'ghost', icon: Clock, label: m.status_job_queued() };
		case 'running':
			return { tone: 'info', icon: LoaderCircle, label: m.status_job_running(), spin: true };
		case 'paused':
			return { tone: 'advisory', icon: Pause, label: m.status_job_paused() };
		case 'stopping':
			return { tone: 'advisory', icon: LoaderCircle, label: m.status_job_stopping(), spin: true };
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

export function pstLook(status: Pst['status']): StatusLook {
	switch (status) {
		case 'uploading':
			return { tone: 'advisory', icon: Upload, label: m.status_pst_uploading() };
		case 'validating':
			return { tone: 'info', icon: ScanSearch, label: m.status_pst_validating(), spin: false };
		case 'valid':
			return { tone: 'signal', icon: CircleCheck, label: m.status_pst_valid() };
		case 'invalid':
			return { tone: 'warning', icon: TriangleAlert, label: m.status_pst_invalid() };
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
			return { tone: 'info', icon: LoaderCircle, label: m.status_upload_uploading(), spin: true };
		case 'completing':
			return { tone: 'info', icon: LoaderCircle, label: m.status_upload_completing(), spin: true };
		case 'paused':
			return { tone: 'advisory', icon: Pause, label: m.status_upload_paused() };
		case 'error':
			return { tone: 'warning', icon: TriangleAlert, label: m.status_upload_error() };
		default:
			return { tone: 'signal', icon: CircleCheck, label: m.status_upload_done() };
	}
}
