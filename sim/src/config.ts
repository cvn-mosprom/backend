/** Explicit process count; 0 keeps the single-process development mode. */
export function workerCount(raw: string | undefined = process.env.SIM_WORKERS): number {
	if (raw === undefined || raw.trim() === '') return 2;
	if (!/^\d+$/.test(raw.trim())) throw new Error('SIM_WORKERS must be an integer from 0 to 256');
	const n = Number(raw);
	if (!Number.isSafeInteger(n) || n > 256) throw new Error('SIM_WORKERS must be an integer from 0 to 256');
	return n;
}
