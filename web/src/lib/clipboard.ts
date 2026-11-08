/**
 * Copy text to the clipboard. Resolves to false when nothing worked, so the caller can ask the user to copy by hand.
 * navigator.clipboard only exists in a secure context (HTTPS or localhost); mccb is often served over plain HTTP on a LAN,
 * where the old textarea + execCommand route is the only one.
 */
export async function copyText(text: string): Promise<boolean> {
	if (window.isSecureContext && navigator.clipboard) {
		try {
			await navigator.clipboard.writeText(text);
			return true;
		} catch {
			// Permission refused: try the old way.
		}
	}
	const active = document.activeElement as HTMLElement | null;
	// Inside a modal <dialog> everything outside it is inert: the textarea must live in the dialog to be selectable.
	const host = active?.closest('dialog') ?? document.body;
	const area = document.createElement('textarea');
	area.value = text;
	area.setAttribute('readonly', '');
	area.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;pointer-events:none';
	host.append(area);
	try {
		area.focus();
		area.select();
		area.setSelectionRange(0, text.length);
		return document.execCommand('copy');
	} catch {
		return false;
	} finally {
		area.remove();
		active?.focus?.();
	}
}
