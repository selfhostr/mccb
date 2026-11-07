/** One timer for every relative time on the page. */
class Clock {
	now = $state(Date.now());
	constructor() {
		setInterval(() => (this.now = Date.now()), 30000);
	}
}

export const clock = new Clock();
