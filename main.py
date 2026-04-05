import asyncio
import logging
from bot import create_bot_and_setup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main():
    bot, dp = create_bot_and_setup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
