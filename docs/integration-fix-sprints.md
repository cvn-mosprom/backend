# Спринты починки стыка бэкенд ↔ фронтенд (S8–S13)

Основа: QA стыка от 24.10.2026 (бэкенд `fe7201f`, фронтенд `422b6fc`): код, сырые HTTP-запросы в форме клиента,
прохождение в браузере.

## Исходное состояние

Бэкенд и фронтенд по отдельности здоровы: e2e бэкенда 209 passed / 4 skipped, vitest фронта 821 passed (3 таймаута под
нагрузкой, в изоляции зелёные). Стыка нет: через бэкенд игра не стартует, игрок видит «ЗАГРУЗКА ЦЕХА… / Нет связи с сервером».

Причины:
1. Две версии контракта. Бэкенд шёл по `docs/ARCHITECTURE.md` и написал свой клиент `clients/web/HttpGameApi.ts`. Фронт
   написал свой `HttpGameApi` по `frontend/docs/http-contract.md` (ревизия 4) и бэкендовский не взял.
2. Сабмодуль `vendor/frontend` закреплён на `dde6cbf` (23.10, 21:03), это на 15 коммитов позади. В этих коммитах вышли
   Contract rev4, новая анкета и квиз, `lib/core`.
3. Sim крутит старый `MockGameApi` через подмену localStorage (`sim/src/host.ts`), а не ядро `lib/core`, которое фронт
   сделал для сервера.

**Источник истины для HTTP — `frontend/docs/http-contract.md` и `frontend/web/src/lib/core/http/fakeApi.ts`** (эталонный
сервер фронта, по нему зелёные интеграционные тесты клиента). Бэкенд подстраивается под клиент, а не наоборот: так клиент
и его тесты остаются как есть.

## Порядок и зависимости

```
S8 транспорт ──► S9 анкета и квиз ──► S10 контрактный тест стыка
       │                                     ▲
       └──► S11 сабмодуль и генератор ──► S12 sim на lib/core ──► S13 хвосты и демо
```

После S8 игра проходится через сервер (кроме анкеты и квиза). После S9 закрыт рекрутинговый сценарий. S10 можно начинать
параллельно с S9: тест сначала падает, потом зеленеет. S11–S12 — технический долг, без них не будет `etaAt` и undo.
Оценка всего: 14–20 часов.

---

## S8. Транспорт: авторизация, CORS, advance — 1–2 ч

Закрывает блокеры 1–3. Без этого спринта клиент не получает ни одного ответа.

**Авторизация** (`api/app/auth.py:104`, `_authenticate`)
- [ ] Принимать заголовки клиента: `X-Telegram-Init-Data: <initData>` → путь `tma`, `X-Dev-User: <id>` → путь `dev`
  (только при `DEV_AUTH=1` и `APP_ENV=dev`). `Authorization: tma|dev` оставить: на нём e2e, бот и README.
- [ ] Порядок: `Authorization`, затем `X-Telegram-Init-Data`, затем `X-Dev-User`. Пустой `X-Telegram-Init-Data` → 401
  `unauthorized`, как сейчас для пустого `tma`.
- [ ] Имя игрока в dev: клиент не шлёт `:<name>`, оставить `Dev<id>`.
- [ ] Тесты в `api/tests` на оба заголовка, на приоритет и на запрет `X-Dev-User` в prod.

**CORS** (`api/app/main.py:55`, `.env.example`, `Caddyfile`)
- [ ] `CORSMiddleware` с источниками из новой переменной `CORS_ORIGINS` (через запятую). Dev по умолчанию
  `http://localhost:5173`; prod — origin из `WEBAPP_URL`.
- [ ] `allow_headers`: `X-Telegram-Init-Data, X-Dev-User, Content-Type, Authorization`; `allow_methods`: `GET, POST, DELETE`;
  `expose_headers`: `Retry-After, X-Request-ID`; `max_age` 600.
- [ ] Preflight `OPTIONS` не проходит auth, rate limit и таймаут-middleware. Проверить порядок middleware: CORS должен стоять
  снаружи `RequestContextMiddleware`, иначе ошибки 4xx/5xx уйдут без CORS-заголовков, и браузер покажет их как «network».
- [ ] Caddy пропускает `OPTIONS` для `/api/*` (сейчас 405 отдаёт FastAPI).

**advance** (`api/app/routes/game.py:45`)
- [ ] `AdvanceIn.reason` принимает `heartbeat` и `order_due` (клиент: `frontend/web/src/lib/ui/game.svelte.ts:288`,
  `nextDue.ts`). В sim отправлять `manual`; `background` остаётся только для бота.
- [ ] Лимит `advance` не ниже 10/мин (клиент держит ≤ 8/мин): сверить с `ratelimit.py`.

**Мелочи контракта**
- [ ] 409 `version_conflict`: добавить `details.version` рядом с `details.current_version` (`api/app/game.py:392`; клиент
  читает `details.version`, `HttpGameApi.ts:218`).
- [ ] `GET /api/bootstrap?start_param=`: брать источник из query, если в initData его нет (вне Telegram сейчас теряется
  `?startapp=` / `?src=` с лендинга). Писать в `users.source` только если там пусто, как сейчас.
- [ ] `features` в bootstrap: добавить `quiz: true`, `leads: true` (сейчас клиент берёт значения по умолчанию).

**Готово когда:** фронт с `VITE_API_URL=https://localhost` (или dev-origin) в обычном браузере без прокси проходит возрастной
гейт, имя цеха и постройку; heartbeat раз в 30 с даёт 200; в консоли нет CORS-ошибок; e2e бэкенда зелёный.

---

## S9. Анкета и квиз под клиентский контракт — 2–3 ч

Закрывает блокеры 4–5. Без этого анкета всегда получает 403, а квиз — 422.

**Анкета** (`api/app/leads.py:71-87` — `LeadIn`, `:286-297` — `lead_out`)
- [ ] Алиасы полей клиента (`frontend/web/src/lib/ui/lead.ts:99-108`): `consentVersion` → `consent_text_version`,
  `tgUsername` → `telegram`, `department` → `direction`. Старые имена оставить для бота и тестов.
- [ ] `department` приходит id (`lab | bottling | tech | warehouse | engineering | other`). Хранить человекочитаемо
  («Лаборатория», «Розлив»…) и сопоставить id с `PROFILE_DIRECTIONS`, чтобы тег `profile_direction` ставился
  (`is_profile_direction`, `leads.py:165`). `other` тег не даёт.
- [ ] `institutionKind` (`vuz | college`): новая колонка `leads.institution_kind`, миграция `0007` (down_revision `0006`).
  Колледж засчитывать в `TAG_SENIOR` так же, как сейчас курс 4+.
- [ ] `GET /api/leads/me` по `LeadInfo` клиента: `createdAt` — ms epoch (число), `roleId` = `vacancy`. Остальные поля
  оставить. Сейчас из-за ISO-строки экран покажет «Отправлена 01.01.1970» (`LeadForm.svelte:102`).
- [ ] HR-карточка в боте: показывать вид учреждения и направление из `department`.

**Квиз** (`api/app/quiz.py`, `api/app/quiz_bank.py`)
- [ ] Решение: один банк вопросов. Рекомендация — сервер берёт банк фронта (`frontend/web/src/lib/api/mock/quiz.ts`:
  8 вопросов `q_year…q_space`, ответы буквами). Правильные ответы и так лежат в бандле клиента, секрета нет, а экраны
  квиза на лендинге и в игре уже построены на этом банке. Читать банк из `vendor/frontend` при сборке, как контракт, а не
  копировать руками.
  Альтернатива: клиент грузит `GET /api/quiz/questions` и шлёт индекс. Это правка фронта (`QuizScreen.svelte`,
  `lib/landing/quiz.ts`), её нужно согласовать с владельцем фронта.
- [ ] `parse_answers`: принимать ответ буквой `a|b|c` и индексом.
- [ ] Ответ `POST /api/quiz/answer` в форме клиента: `{correct, total, score: 0..100, results: [{question_id, correct}]}`.
  Поля `answered` и `completed` оставить. Внимание: сейчас `score` — число верных; бот (`/funnel`, теги) читает БД,
  а не этот ответ, но сверить и `quiz_result` в summary.
- [ ] `HIGH_SCORE` пересчитать под 8 вопросов (`quiz_bank.py:59`).
- [ ] Повтор после полного прохождения → 409 `quiz_already_completed` (клиент его ждёт).
- [ ] Миграция данных: ответы на старые `q1978…` в `quiz_answers` либо удалить на dev, либо оставить и не считать.

**Готово когда:** анкета из игры с одним Telegram-контактом → 201, HR получает карточку с Telegram и направлением, экран
анкеты показывает верную дату; квиз из игры даёт результат, повтор — 409; анкета работает при упавшем sim (как в S5).

---

## S10. Контрактный тест стыка — 2 ч

Чтобы расхождение больше не прошло незамеченным: сейчас обе стороны тестируют себя только против своих представлений о
контракте.

- [ ] `e2e/client/`: node-сценарии, которые импортируют **настоящий** `HttpGameApi` из `vendor/frontend` и гоняют его
  против живого стека (`E2E_BASE`): bootstrap → place → advance(`heartbeat`, `order_due`) → устаревшая версия → 409 и
  ресинк → повтор по обрыву (`replayed`) → leaderboard (4 таба) → visit → summary → анкета → getLead → квиз → events →
  revokeLead.
- [ ] Сырой тест транспорта: preflight с `Origin` dev-фронта, проверка CORS-заголовков на 200 и на 4xx/5xx, оба
  auth-заголовка.
- [ ] Запуск в README (раздел Tests) и в общем прогоне e2e.
- [ ] Удалить или пометить устаревшим `clients/web/HttpGameApi.ts` и `clients/web/README.md`: фронт использует свой клиент,
  второй вводит в заблуждение.
- [ ] `docs/ARCHITECTURE.md`: разделы Auth, S2 (advance), S5 (leads, quiz) привести к `http-contract.md` и сослаться на
  него как на источник истины.

**Готово когда:** тест падает на коде до S8 и S9 и зеленеет после; любое переименование поля на любой стороне ломает его.

---

## S11. Сабмодуль и генератор контракта — 2–3 ч

- [ ] `contract/generate.mjs:77`: генератор падает на текущем `types.ts` (`SimEvent: expected an anyOf union`) — из
  `Command` убран `tick`, `SimEvent` больше не union в контракте. Сделать постобработку терпимой к отсутствующим и
  не-union определениям.
- [ ] Поднять `vendor/frontend` до HEAD **ветки `main`** фронтенда (не до рабочей ветки лендинга), перегенерировать схемы
  и Pydantic (`contract/generate.sh`), прогнать `contract/check.sh`.
- [ ] Что меняется в контракте: из `Command` уходит `tick` (сервер ответит 422 `validation_error` вместо `tick_removed` —
  поправить e2e `test_h_undo_redo_tick_rejected`), `LeadForm` новой формы, `Order.etaAt?`, `GameSnapshot.version?`.
- [ ] Обновить `contract/examples`, контрактные тесты, `SIM_COMMIT`.

**Готово когда:** `contract/check.sh` зелёный на новом пине, api-тесты и e2e зелёные, образ sim помечен новым коммитом.

---

## S12. Sim на ядре `lib/core` — 4–6 ч

Самый крупный спринт. Без него сервер считает игру старым кодом: нет `etaAt`, нет undo, а картинка клиента (задержка
заполнения линии, расписание заказов) расходится с цифрами сервера.

- [ ] `sim/src/engine.ts` и `sim/src/host.ts`: вместо `MockGameApi` с подменой localStorage звать чистые функции
  `$lib/core` — `migrateState`, `freshState`, `advanceState`, `applyCommand`, `snapshotOf` (контракт ядра:
  `frontend/web/src/lib/core/README.md`). Время передаётся параметром, `max(now, state.lastTick)` оставить.
- [ ] Миграция состояний: каждая загрузка проходит через `migrateState` (лениво, на месте). Тест на снимках реальных
  состояний из текущей БД: `pg_dump` dev-стека с цехами e2e и ботами.
- [ ] Неизвестные поля верхнего уровня (`meta.idempotency`, `meta.speed`) ядро должно сохранять как есть: контрактный тест
  уже есть, прогнать на новом ядре.
- [ ] Коды отказов брать из `r.code` ядра, а не разбором текста (`sim/src/reasons.ts`). Таблицу сверить с кодами, которые
  API выносит наверх (`daily_limit_exceeded`, `minigame_too_short`, `impossible_score`).
- [ ] Убрать `undo`/`redo` из `REJECTED_COMMANDS` (`sim/src/engine.ts:68`): история живёт в состоянии. `/health.historyInState`
  станет `true` → `features.undo = true`.
- [ ] `features.serverOrderEta = true`; `etaAt` у каждого заказа в `loading`.
- [ ] Офлайн-отчёт фонового досчёта бота (`background`) должен копиться, а не затираться: проверить, что ядро это умеет,
  иначе оставить обёртку в sim.
- [ ] `speed_bph`: если ядро отдаёт поток (`bottlesPerHour`), брать его вместо вычисления по дельте (`ARCHITECTURE.md`, S4).
- [ ] Аудит размера состояния ≤ 150 КБ: снять `xfail` с теста.
- [ ] Боты: в ядре есть свои детерминированные боты (`lib/core/bots.ts`). Оставить серверный `seed_bots` и задокументировать
  выбор.

**Готово когда:** тесты sim и контрактный тест снимка на живом sim зелёные, туториал e2e проходит, undo/redo работают по
HTTP и переживают перезапрос, `etaAt` приходит и сдвигается при перестройке, старые цехи грузятся без потерь.

---

## S13. Хвосты и подготовка к демо — 1–2 ч

- [ ] Раздача веб-сборки тем же Caddy (`adapter-static` → `handle /*`, `/api/*` → api): фронт и API на одном origin, в prod
  CORS не нужен, Telegram открывает `https://DOMAIN/`. Обновить `WEBAPP_URL` и README.
- [ ] `POST /api/events`: незнакомое имя сейчас роняет всю пачку (422), а клиент такую пачку выбрасывает навсегда. Принимать
  известные события, незнакомые отбрасывать и возвращать `{accepted, rejected}`.
- [ ] Тестовые пользователи e2e попадают в топ-50 («e2e · Мой цех», 0★). Метить их (`users.is_test` или диапазон `tg_id`)
  и исключать из рейтинга и аналитики, как ботов; либо чистить базу перед демо.
- [ ] `README.md` (для жюри), `PROGRESS.md`, `docs/ARCHITECTURE.md`: отразить S8–S12, убрать «Waiting for the lead: S1/S1U»
  (фронт это сделал).
- [ ] Прогон демо-сценария в настоящем Telegram на домене: вход, туториал, заказ, смена, топ, анкета, квиз, карточка
  в HR-боте.

**Готово когда:** демо из README проходится в Telegram от начала до конца без ручного вмешательства.

---

## Не бэкенд: передать владельцу фронта

- Окно туториала перекрывает низ анкеты вместе с кнопкой «ОТПРАВИТЬ»: пока диалог не свёрнут, отправить форму нельзя.
- Текст ошибки квиза «Не удалось отправить ответы, попробуй ещё раз» на 4xx: повтор не поможет, нужен текст по коду.
- Если в S9 выбран вариант с `GET /api/quiz/questions`: перевести `QuizScreen.svelte` и `lib/landing/quiz.ts` на вопросы
  сервера.
