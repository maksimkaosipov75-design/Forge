import asyncio
import logging

from bot import create_bot_and_setup
from process_manager import CodexProcessManager
from parser import LogParser
from file_manager import FileManager


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main():
    parser = LogParser()
    file_mgr = FileManager(projects_file=".session_data/projects_codex_seed.json")
    manager = CodexProcessManager(cli_path="codex", on_output=lambda line: parser.feed(line))
    bot, dp = create_bot_and_setup(manager=manager, parser=parser, file_mgr=file_mgr)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
