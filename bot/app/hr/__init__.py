"""HR bot (S6): lead cards with status buttons, /leads /lead /export /funnel /sources /stats /top, outbox delivery.

The bot reads the database directly with SQLAlchemy (async, same DATABASE_URL / PGBOUNCER semantics as the api) and
never touches ``factories.state``. Demo bots (``users.is_bot``) are excluded from every HR number, the CSV and the
lead list; /top shows them (marked) only while BOTS_ENABLED. Only active ``hr_users`` (C3, see ``access.py``) may use it; ``HR_ADMIN_TG_IDS`` is the fallback while that table is empty.
"""
