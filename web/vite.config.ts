import { paraglideVitePlugin } from '@inlang/paraglide-js';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

const API = { target: process.env.MCCB_DEV_API ?? 'http://127.0.0.1:22362', changeOrigin: true };

export default defineConfig({
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
		})
	],
	// FastAPI serves app/web (settings.web_dir): the folder is generated, see .gitignore.
	build: { outDir: '../app/web', emptyOutDir: true, assetsDir: 'assets' },
	server: { proxy: { '/api': API } },
	preview: { proxy: { '/api': API } }
});
