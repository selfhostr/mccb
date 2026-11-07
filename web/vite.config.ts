import { copyFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { paraglideVitePlugin } from '@inlang/paraglide-js';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig, type Plugin } from 'vite';

const API = { target: process.env.MCCB_DEV_API ?? 'http://127.0.0.1:22362', changeOrigin: true };

/** Deep links of the demo: the static host serves the nearest 404.html, which is the app itself. */
function demoDeepLinks(outDir: string): Plugin {
	return {
		name: 'mccb-demo-deep-links',
		apply: 'build',
		closeBundle() {
			const dir = fileURLToPath(new URL(`./${outDir}/`, import.meta.url));
			copyFileSync(`${dir}index.html`, `${dir}404.html`);
		}
	};
}

// `vite build --mode demo` (npm run build:demo) makes the static demo: the same UI under /demo/, the API simulated in
// the browser. Every other mode is the product. __DEMO__ is a constant, so the product keeps none of the demo.
export default defineConfig(({ mode }) => {
	const demo = mode === 'demo';
	return {
		base: demo ? '/demo/' : '/',
		define: { __DEMO__: JSON.stringify(demo) },
		plugins: [
			tailwindcss(),
			svelte(),
			// Physical files in src/lib/paraglide, generated and never committed.
			paraglideVitePlugin({
				project: './project.inlang',
				outdir: './src/lib/paraglide',
				strategy: ['localStorage', 'preferredLanguage', 'baseLocale'],
				outputStructure: 'locale-modules',
				emitTsDeclarations: true
			}),
			...(demo ? [demoDeepLinks('dist-demo')] : [])
		],
		// FastAPI serves app/web (settings.web_dir): the folder is generated, see .gitignore.
		// The demo inlines no asset as a data: URL, so that its CSP needs neither img-src nor font-src data:.
		build: { outDir: demo ? 'dist-demo' : '../app/web', emptyOutDir: true, assetsDir: 'assets', ...(demo ? { assetsInlineLimit: 0 } : {}) },
		server: { proxy: { '/api': API } },
		preview: { proxy: { '/api': API } }
	};
});
