<script lang="ts">
	/** Lines have one fixed height, so even a very long log renders only what shows. */
	import Download from '@lucide/svelte/icons/download';
	import { onDestroy } from 'svelte';
	import { m } from '#lib/paraglide/messages.js';
	import { errorText, get } from '../../lib/api/client.ts';
	import type { LogLine } from '../../lib/api/types.ts';
	import { fmtAbsolute, toDate } from '../../lib/format.ts';
	import { windowRange } from '../../lib/logic/virtual.ts';
	import { prefs } from '../../lib/prefs.svelte.ts';
	import CopyButton from '../../lib/ui/CopyButton.svelte';
	import ErrorNotice from '../../lib/ui/ErrorNotice.svelte';
	import IconButton from '../../lib/ui/IconButton.svelte';

	let { jobId, active }: { jobId: number; active: boolean } = $props();

	const LINE_H = 20;
	const MAX_LINES = 50000;
	const VIEW_H = 320;

	let lines = $state.raw<LogLine[]>([]);
	let level = $state('');
	let error = $state<string | null>(null);
	let box = $state<HTMLElement>();
	let top = $state(0);
	let follow = true;
	let after = 0;
	let timer: ReturnType<typeof setTimeout> | undefined;
	let stopped = false;

	const filtered = $derived(
		level === 'error' ? lines.filter((l) => /err/i.test(l.level)) : level === 'warn' ? lines.filter((l) => /warn|err/i.test(l.level)) : lines
	);
	const range = $derived(windowRange(filtered.length, top, VIEW_H, LINE_H, 20, 400));
	const shown = $derived(filtered.slice(range.start, range.end));

	const format = (l: LogLine) => `${fmtAbsolute(toDate(l.ts))}  ${(l.level ?? 'info').toUpperCase()}  ${prefs.text(l.msg)}`;
	const asText = () => filtered.map(format).join('\n');

	async function poll() {
		try {
			const res = await get<{ lines: LogLine[]; next: number }>(`/jobs/${jobId}/log?after=${after}`);
			if (stopped) return;
			error = null;
			if (res.lines.length) {
				after = res.next;
				const merged = lines.concat(res.lines);
				lines = merged.length > MAX_LINES ? merged.slice(merged.length - MAX_LINES) : merged;
				if (follow) requestAnimationFrame(() => box && (box.scrollTop = box.scrollHeight));
			}
		} catch (e) {
			if (!stopped) error = errorText(e);
		}
		if (!stopped) timer = setTimeout(poll, active ? 1500 : 5000);
	}

	function onscroll() {
		if (!box) return;
		top = box.scrollTop;
		follow = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
	}

	function download() {
		const url = URL.createObjectURL(new Blob([asText()], { type: 'text/plain' }));
		const a = document.createElement('a');
		a.href = url;
		a.download = `job-${jobId}.log`;
		a.click();
		setTimeout(() => URL.revokeObjectURL(url), 1000);
	}

	void poll();
	onDestroy(() => {
		stopped = true;
		clearTimeout(timer);
	});
</script>

<div class="flex items-center gap-2">
	<select class="input w-auto" aria-label={m.jobs_log_level()} bind:value={level}>
		<option value="">{m.jobs_log_all()}</option>
		<option value="warn">{m.jobs_log_warnings()}</option>
		<option value="error">{m.jobs_log_errors()}</option>
	</select>
	<span class="flex-1"></span>
	<CopyButton text={asText} label={m.jobs_log_copy()} />
	<IconButton icon={Download} label={m.jobs_log_download()} onclick={download} />
</div>

{#if error}<ErrorNotice title={m.jobs_log_failed()} message={error} />{/if}

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div bind:this={box} {onscroll} class="overflow-auto rounded-btn border border-line bg-canvas-deep font-mono text-xs" style:height={`${VIEW_H}px`} tabindex="0" role="log" aria-label={m.jobs_tab_log()} data-testid="job-log">
	{#if !filtered.length}
		<p class="m-0 p-3 text-ink-3">{m.jobs_log_empty()}</p>
	{:else}
		<div style:height={`${range.before}px`}></div>
		{#each shown as line (line.n)}
			<div class={`px-2 whitespace-pre ${/err/i.test(line.level) ? 'text-warning-ink' : /warn/i.test(line.level) ? 'text-advisory-ink' : 'text-ink-2'}`} style:height={`${LINE_H}px`} style:line-height={`${LINE_H}px`}>{format(line)}</div>
		{/each}
		<div style:height={`${range.after}px`}></div>
	{/if}
</div>
