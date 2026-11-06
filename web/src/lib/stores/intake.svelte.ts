import { errorText, get, put } from '../api/client.ts';
import type { IntakeSettings, IntakeSettingsPatch } from '../api/types.ts';

/** Notification, drop folder and automatic assignment settings: one document, read and patched as a whole. */
class Intake {
	settings = $state.raw<IntakeSettings | null>(null);
	error = $state<string | null>(null);
	loading = $state(false);

	async load(): Promise<void> {
		this.loading = true;
		try {
			this.settings = await get<IntakeSettings>('/intake/settings');
			this.error = null;
		} catch (error) {
			this.error = errorText(error);
		} finally {
			this.loading = false;
		}
	}

	/** Partial update. Resolves to the stored settings; a refusal throws with the server's message. */
	async save(patch: IntakeSettingsPatch): Promise<IntakeSettings> {
		const next = await put<IntakeSettings>('/intake/settings', patch);
		this.settings = next;
		return next;
	}
}

export const intake = new Intake();
