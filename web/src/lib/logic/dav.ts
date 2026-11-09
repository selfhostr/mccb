import type { Server, TestResult } from '../api/types.ts';

/** DAV is switched off on this server although its last test found CalDAV or CardDAV answering. */
export function davDetected(server: Pick<Server, 'dav_enabled'>, result: TestResult | null | undefined): boolean {
	return !server.dav_enabled && !!result && (result.caldav?.found === true || result.carddav?.found === true);
}
