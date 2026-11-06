const DOT = '•••';

function maskLabel(label: string): string {
	return label ? `${label[0]}${DOT}` : DOT;
}

export function maskEmail(value: string): string {
	const at = value.lastIndexOf('@');
	if (at < 0) return maskLabel(value);
	const domain = value.slice(at + 1);
	const dot = domain.lastIndexOf('.');
	const tail = dot >= 0 ? domain.slice(dot) : '';
	return `${maskLabel(value.slice(0, at))}@${maskLabel(dot >= 0 ? domain.slice(0, dot) : domain)}${tail}`;
}

export function maskHost(value: string): string {
	if (/^\d+\.\d+\.\d+\.\d+$/.test(value) || value.includes(':')) return DOT;
	const dot = value.lastIndexOf('.');
	return dot > 0 ? `${maskLabel(value)}${value.slice(dot)}` : maskLabel(value);
}

export function maskFile(name: string): string {
	const dot = name.lastIndexOf('.');
	return dot > 0 ? `${maskLabel(name)}${name.slice(dot)}` : maskLabel(name);
}

const EMAIL = /[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g;
const URL_HOST = /(https?:\/\/)([^/\s:]+)/g;
const IPV4 = /\b\d{1,3}(?:\.\d{1,3}){3}\b/g;

/** Free text from the server: logs, error messages, test results. */
export function maskText(text: string): string {
	return text
		.replace(EMAIL, (m) => maskEmail(m))
		.replace(URL_HOST, (_m, scheme: string, host: string) => `${scheme}${maskHost(host)}`)
		.replace(IPV4, DOT);
}
