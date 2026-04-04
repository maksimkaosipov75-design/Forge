import asyncio
import logging
from bot import create_bot_and_setup
from process_manager import QwenProcessManager
from parser import LogParser
from file_manager import FileManager
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

parser = LogParser()
file_mgr = FileManager()
manager = QwenProcessManager(cli_path=settings.CLI_PATH, on_output=lambda line: parser.feed(line))


async def main():
    bot, dp = create_bot_and_setup(manager, parser, file_mgr)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
