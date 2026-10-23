// Host for the vendored MockGameApi ("the reference server").
//
// The mock keeps its whole persisted state as JSON in `localStorage['ochakovo-shift-v1']` and reads the wall clock
// through `Date.now()`. To use it as a stateless server function we, per request:
//   1. install an in-memory `localStorage` shim holding the incoming state (with `clock` pinned to the request time),
//   2. freeze `Date.now()` at the requested server time (so `mock.now()` is exactly the requested `now`, and
//      `lastSeenReal` / `realGap` are measured in server time),
//   3. construct `new MockGameApi()`, run the operation, read the state back from the shim,
//   4. restore the globals.
// Steps 1-4 run inside a process-wide mutex (`exclusive`). The mock has no real I/O, so all of it is synchronous CPU
// work and the mutex costs nothing; it only guarantees that two requests can never see each other's globals, even if
// a future vendor version introduces `await`s between microtasks.

import { MockGameApi } from '$lib/api/mock/MockGameApi';

/** localStorage key the mock persists under (MockGameApi.ts `KEY`). */
export const MOCK_STATE_KEY = 'ochakovo-shift-v1';

/** Persisted factory state: the mock's `MockState` plus any unknown top-level fields (e.g. `meta`). */
export type StateObject = Record<string, unknown>;

class MemoryStorage {
	private readonly m = new Map<string, string>();
	get length() {
		return this.m.size;
	}
	getItem(k: string): string | null {
		return this.m.has(k) ? this.m.get(k)! : null;
	}
	setItem(k: string, v: string): void {
		this.m.set(k, String(v));
	}
	removeItem(k: string): void {
		this.m.delete(k);
	}
	clear(): void {
		this.m.clear();
	}
	key(i: number): string | null {
		return [...this.m.keys()][i] ?? null;
	}
}

let chain: Promise<unknown> = Promise.resolve();

/** Run `fn` after every previously queued `exclusive` call has settled. */
export function exclusive<T>(fn: () => Promise<T> | T): Promise<T> {
	const run = chain.then(fn);
	chain = run.then(
		() => undefined,
		() => undefined
	);
	return run;
}

function installStorage(storage: MemoryStorage): () => void {
	const g = globalThis as Record<string, unknown>;
	const prev = Object.getOwnPropertyDescriptor(g, 'localStorage');
	Object.defineProperty(g, 'localStorage', { value: storage, configurable: true, writable: true, enumerable: false });
	return () => {
		if (prev) Object.defineProperty(g, 'localStorage', prev);
		else delete g.localStorage;
	};
}

/** The state exactly as the mock expects to find it for a request at server time `now`. */
export function prepareState(state: StateObject, now: number): StateObject {
	const prepared: StateObject = { ...state, clock: { real: now, server: now, scale: 1 } };
	// The discrete-sim "client is online" marker is a real-time field of the removed `tick` command; the server is
	// always analytic. Dropping it keeps `online()` false regardless of what an imported save carried.
	delete prepared.discReal;
	return prepared;
}

/**
 * Run `fn` against a fresh MockGameApi whose storage holds `state` (or nothing: the mock then creates a new
 * factory). Returns the value and the state the mock persisted.
 */
export function withMock<T>(
	state: StateObject | null,
	now: number,
	fn: (mock: MockGameApi) => Promise<T> | T
): Promise<{ value: T; state: StateObject }> {
	return exclusive(async () => {
		const storage = new MemoryStorage();
		if (state) storage.setItem(MOCK_STATE_KEY, JSON.stringify(prepareState(state, now)));
		const restoreStorage = installStorage(storage);
		const realNow = Date.now;
		Date.now = () => now;
		try {
			const mock = new MockGameApi();
			const value = await fn(mock);
			const raw = storage.getItem(MOCK_STATE_KEY);
			if (raw === null) throw new Error('MockGameApi did not persist a state');
			return { value, state: JSON.parse(raw) as StateObject };
		} finally {
			Date.now = realNow;
			restoreStorage();
		}
	});
}
