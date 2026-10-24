import asyncio
from unittest.mock import patch

import pytest

from app import lease as module


def test_loss_of_ownership_cancels_polling_and_scheduler_before_releasing():
    # the bot suite has no pytest-asyncio: drive the coroutine with asyncio.run like the other bot tests
    asyncio.run(_loss_of_ownership())


async def _loss_of_ownership():
    events = []
    original_sleep = asyncio.sleep

    class Client:
        async def aclose(self):
            events.append("closed")

    class Lease:
        client = Client()

        async def acquire(self):
            events.append("acquired")
            return True

        async def renew(self):
            raise RuntimeError("lost")

        async def release(self):
            events.append("released")

    async def polling(stop):
        events.append("started")
        try:
            await stop.wait()
        finally:
            events.append("stopped")

    async def tick(_):
        await original_sleep(0)

    with patch.object(module, "Lease", Lease), patch.object(module.asyncio, "sleep", tick):
        with pytest.raises(RuntimeError, match="lost"):
            await module.run_with_lease(polling)
    assert events.index("acquired") < events.index("started") < events.index("stopped") < events.index("released") < events.index("closed")
