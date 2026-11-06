// Applied before the first paint so that a saved theme or density never flashes.
try {
	var saved = JSON.parse(localStorage.getItem('mccb.prefs.v1') || '{}');
	if (saved.theme === 'light') document.documentElement.dataset.theme = 'light';
	if (saved.density === 'comfortable') document.documentElement.dataset.density = 'comfortable';
} catch (e) {
	// No storage or invalid JSON: keep the defaults.
}
