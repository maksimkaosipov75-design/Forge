import asyncio
import logging
import signal
from bot import create_bot_and_setup
from process_manager import QwenProcessManager
from parser import LogParser
from file_manager import FileManager
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

parser = LogParser()
file_mgr = FileManager()
manager = QwenProcessManager(cli_path=settings.CLI_PATH, on_output=lambda line: parser.feed(line))


async def main():
    bot, dp = create_bot_and_setup(manager, parser, file_mgr)

    loop = asyncio.get_running_loop()

    async def shutdown():
        log.info("Завершение работы...")
        await manager.stop()
        await dp.stop_polling()
        await bot.session.close()

    def handle_signal():
        asyncio.ensure_future(shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    try:
        await dp.start_polling(bot)
    finally:
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
