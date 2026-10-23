# HttpGameApi (frontend client for the S2 API)

`HttpGameApi.ts` implements the frontend's `GameApi` (`web/src/lib/api/types.ts`) over the HTTP API. Copy it to
`web/src/lib/api/HttpGameApi.ts` (it only imports types via `$lib/api/types`; no dependencies).

## Plug in

`web/src/lib/api/index.ts` currently returns `new MockGameApi()` from `getApi()` and exposes `getDev()`. Switch:

```ts
import type { GameApi } from './types';
import { MockGameApi } from './mock/MockGameApi';
import { HttpGameApi } from './HttpGameApi';

const USE_HTTP = import.meta.env.VITE_API !== 'mock';          // keep the mock for offline dev
let api: GameApi | undefined;

export function getApi(): GameApi {
	return (api ??= USE_HTTP ? new HttpGameApi({ baseUrl: import.meta.env.VITE_API_BASE ?? '' }) : new MockGameApi());
}
/** Dev-управление существует только у мока. */
export function getDev(): MockGameApi | undefined {
	const a = getApi();
	return a instanceof MockGameApi ? a : undefined;
}
export function getFeatures() {                                // undo/redo flag for the UI
	const a = getApi();
	return a instanceof HttpGameApi ? a.features : { undo: true, serverOrderEta: false };
}
export { onResume } from './HttpGameApi';
```

Auth: inside Telegram it sends `Authorization: tma <WebApp.initData>`; otherwise `dev <id>` (random id kept in
`localStorage['mosprom.devTgId']`, server needs `DEV_AUTH=1`). Set `VITE_API_BASE` only when the API is on another origin.

## Required frontend changes (small)

1. **Stop sending `tick`.** `game.svelte.ts: startSync()` calls `sendTick(true)` first and returns early when it
   flushed, so the poll would never run. Make the interval just `this.setSnap(await getApi().bootstrap())` (drop the
   `sendTick` branch). `HttpGameApi.command({type:'tick'})` answers `{ok:false, error:'tick_removed'}` locally.
2. **Polling is safe as is.** `bootstrap()` hits `GET /api/bootstrap` once; later calls turn into
   `GET /api/factory/me`, throttled to one request per 4 s (`minPollMs`), so the existing 1 s loop is fine. `factory/me`
   never returns 409 and does not bump the version, so two tabs polling do not break each other's commands.
3. **Resume from background.** The client already resyncs on `visibilitychange`; for the UI store pass
   `new HttpGameApi({ onSnapshot: (s) => game.setSnap(s) })` or call `onResume()` yourself.
4. **Orders:** loading and closing are done by the server; the frontend must not send loading/closing commands (it
   does not: only `acceptOrder` / `cancelOrder`).

## Buttons / controls to hide on HttpGameApi

| Control | Where | Rule |
|---|---|---|
| Undo / Redo buttons, "Отменить" offer toast, Ctrl+Z, quick-undo | `routes/+page.svelte` (corner undo button, `undo-offer`), `BuildSheet.svelte:57`, `SceneView.svelte:116` | hide while `!getFeatures().undo` (server answers 422 `history_unavailable` until the S1U history migration; the client also refuses to send them locally). Note `snap.undoSteps` is 0 on the server, so the buttons are already disabled; hide them anyway |
| Dev panel (time scale, skip hours, add credits, set level, end season, reset shifts, reset) | `DevPanel.svelte`, `devtools.ts`, `routes/+page.svelte` dev block | `getDev()` is `undefined` on HTTP: make `game.dev` (`game.svelte.ts:70`) `&& !!getDev()`, otherwise `DevPanel` (`getDev()!`) throws |

## Error mapping

| HTTP | Result |
|---|---|
| 200 | `{ok:true, snapshot, toast, rewards}`; `buildEnd` keeps the previous snapshot and the same version |
| 422 (`command_rejected`, `daily_limit_exceeded`, `minigame_too_short`, `impossible_score`, `validation_error`) | `{ok:false, error: details.message, snapshot: details.snapshot}` |
| 409 `version_conflict` | `GET /api/factory/me`, update version, retry once with the same idempotency key |
| 429 | wait `Retry-After` (0.5..10 s) once, retry with the same key |
| network error / 502-504 | up to 2 retries (0.3 s, 0.9 s) with the same key |

`leaderboard`, `visit`, `seasonSummary`, `submitLead`, `track` call `GET /api/season/leaderboard?tab=`,
`GET /api/factory/{id}`, `GET /api/season/summary`, `POST /api/leads`, `POST /api/events {events:[...]}`. These
endpoints land in later sprints: response bodies are passed through as-is, so adapt field names there when they exist
(`submitLead` returns `{ok:false}` on 403/409/422; `track` never throws).

## Typecheck

```
cd clients/web && npm install && npm run typecheck     # tsc --noEmit, $lib/* -> ../../vendor/frontend/web/src/lib/*
```
