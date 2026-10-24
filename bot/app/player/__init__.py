"""Player-facing bot features (S6): reminder consent (/start toggle, /notify, /stop), order reminders, 5xx alert.

* ``store``     SQL of the bot's own writes to ``users`` (bot_started_at, notify_*) and the ``reminders_sent`` dedupe table.
* ``notify``    router: opt-in / opt-out callbacks and the /notify, /stop commands.
* ``reminders`` job: reads ``factories.snapshot -> orders`` (read-only, never ``factories.state``) of opted-in players.
* ``alerts``    job: sums the api's per-minute request counters in Redis and warns the HR admins about a 5xx storm.
* ``runtime``   process-wide handles (Bot, DB sessionmaker, Redis) used by the scheduled jobs.
"""
