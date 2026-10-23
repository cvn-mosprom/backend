/** SIM_WORKERS: worker processes behind the shared port. Default 2, max 4; 0 = single in-process server. */
export function workerCount(raw: string | undefined = process.env.SIM_WORKERS): number {
	if (raw === undefined || raw.trim() === '') return 2;
	const n = Number.parseInt(raw, 10);
	if (!Number.isFinite(n) || n < 0) return 2;
	return Math.min(n, 4);
}
