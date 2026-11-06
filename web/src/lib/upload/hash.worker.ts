import { sha256Js } from './sha256.ts';

interface HashRequest {
	id: number;
	body: ArrayBuffer;
}

const scope = self as unknown as {
	onmessage: ((event: MessageEvent<HashRequest>) => void) | null;
	postMessage(message: unknown): void;
};

scope.onmessage = (event) => {
	scope.postMessage({ id: event.data.id, sha256: sha256Js(event.data.body) });
};
