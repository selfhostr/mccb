import './app.css';
import { type Component, mount } from 'svelte';
import App from './App.svelte';

async function start() {
	let extra: Component<{ part: 'badge' | 'footer' }> | undefined;
	// Only the build of the static demo (npm run build:demo): the condition is a constant, so the product's
	// bundle neither contains nor even loads the simulated API.
	if (__DEMO__) extra = await (await import('./demo/install.ts')).installDemo();
	mount(App, { target: document.getElementById('app') as HTMLElement, props: { extra } });
}

void start();
