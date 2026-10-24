// Caddy serves its own local CA for https://localhost; Node's fetch must accept it.
process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

// Drift check (opt-in): E2E_MUTATE="etaAt:eta_at,score:points" renames those keys, at any depth, in every JSON answer of the server before the
// client sees it - a stand-in for "the server renamed a field". The suite must go red: `E2E_MUTATE=etaAt:eta_at npx vitest run`.
const spec = process.env.E2E_MUTATE;
if (spec) {
	const map = new Map(spec.split(',').map((p) => p.split(':') as [string, string]));
	const rename = (v: unknown): unknown =>
		Array.isArray(v) ? v.map(rename) : v && typeof v === 'object' ? Object.fromEntries(Object.entries(v).map(([k, x]) => [map.get(k) ?? k, rename(x)])) : v;
	const orig = globalThis.fetch;
	globalThis.fetch = async (input, init) => {
		const res = await orig(input, init);
		if (!res.headers.get('content-type')?.includes('json')) return res;
		const text = await res.text();
		let body = text;
		try { body = JSON.stringify(rename(JSON.parse(text))); } catch { /* not JSON after all */ }
		const headers = new Headers(res.headers);
		headers.delete('content-length');
		headers.delete('content-encoding');
		return new Response(body, { status: res.status, statusText: res.statusText, headers });
	};
}
