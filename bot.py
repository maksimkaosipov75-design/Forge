import asyncio
import logging
from pathlib import Path
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from process_manager import QwenProcessManager
from parser import LogParser
from file_manager import FileManager
from config import settings

log = logging.getLogger(__name__)
router = Router()


def create_bot_and_setup(manager: QwenProcessManager, parser: LogParser, file_mgr: FileManager):
    bot = Bot(token=settings.TELEGRAM_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # --- File change monitoring ---
    last_file_state: dict[Path, float] = {}

    def _scan_dir(directory: Path) -> dict[Path, float]:
        """Сканирует директорию и возвращает {файл: mtime}."""
        result = {}
        try:
            for f in directory.rglob("*"):
                if f.is_file() and not any(part.startswith(".") for part in f.parts):
                    try:
                        result[f] = f.stat().st_mtime
                    except OSError:
                        pass
        except PermissionError:
            pass
        return result

    async def _send_long_message(message: Message, text: str):
        """Отправляет длинное сообщение, разбивая на чанки по 4000 символов."""
        max_len = 4000
        while text:
            chunk = text[:max_len]
            # Не режем посреди строки
            if len(text) > max_len:
                last_newline = chunk.rfind("\n")
                if last_newline > max_len * 0.7:
                    chunk = chunk[:last_newline]
            await message.answer(chunk)
            text = text[len(chunk):]
            if text:
                await asyncio.sleep(0.5)  # Небольшая пауза между чанками

    async def _run_task(prompt: str, message: Message, status_msg: Message):
        """Запускает задачу qwen со стримингом в Telegram."""
        work_dir = file_mgr.get_working_dir()

        # Начальное состояние файлов
        nonlocal last_file_state
        last_file_state = _scan_dir(work_dir)

        # Буфер для стриминга
        stream_lines: list[str] = []
        stream_lock = asyncio.Lock()
        task_done = asyncio.Event()
        returncode_holder = [0]  # mutable container for returncode

        def on_stream_line(line: str):
            """Колбэк для каждой строки от qwen."""
            actionable = parser.get_actionable_line(line)
            if actionable:
                stream_lines.append(actionable)
            elif line and len(line) > 2:
                stripped = line.strip()
                # Фильтруем UI-мусор
                skip_prefixes = ("─", "│", "┌", "└", "┐", "┘", "Tips:", "esc esc",
                                 "ctrl+", "shift+", "Qwen OAuth", "/model")
                if not any(stripped.startswith(p) for p in skip_prefixes):
                    # Только содержательные строки
                    if len(stripped) > 3:
                        stream_lines.append(stripped)

        # Устанавливаем колбэк стриминга
        manager.set_stream_callback(on_stream_line)
        manager.set_final_result_callback(lambda text: parser.set_final_result(text))

        async def update_status_loop():
            """Обновляет статус-сообщение каждые 3 секунды."""
            while not task_done.is_set():
                await asyncio.sleep(3)

                async with stream_lock:
                    if not stream_lines:
                        continue

                    # Берём последние 10 действий
                    recent = stream_lines[-10:]
                    text = "\n".join(recent)
                    if len(text) > 4000:
                        text = text[:4000] + "..."

                try:
                    await status_msg.edit_text(f"⏳ Выполняю:\n\n{text}")
                except Exception:
                    pass  # Сообщение могло быть удалено

        async def run_qwen():
            """Запускает qwen и запоминает код возврата."""
            try:
                rc = await manager.send_command(prompt, cwd=work_dir)
                returncode_holder[0] = rc
            except Exception as e:
                log.error(f"Ошибка запуска: {e}", exc_info=True)
                returncode_holder[0] = -1
            finally:
                task_done.set()

        # Запускаем параллельно: qwen + обновление статуса
        qwen_task = asyncio.create_task(run_qwen())
        status_task = asyncio.create_task(update_status_loop())

        await task_done  # Ждём завершения qwen

        # Финальное обновление статуса
        await asyncio.sleep(1)
        async with stream_lock:
            if stream_lines:
                recent = stream_lines[-10:]
                text = "\n".join(recent)
                if len(text) > 4000:
                    text = text[:4000] + "..."
                try:
                    await status_msg.edit_text(f"✅ Выполнено:\n\n{text}")
                except Exception:
                    pass

        # Отменяем задачу обновления
        status_task.cancel()
        try:
            await status_task
        except asyncio.CancelledError:
            pass

        # Сбрасываем колбэк
        manager.set_stream_callback(None)

        # Результат
        returncode = returncode_holder[0]
        if returncode == 0:
            # Изменения файлов
            current = _scan_dir(work_dir)
            new_files = set(current.keys()) - set(last_file_state.keys())
            changed_files = {
                f for f, t in current.items()
                if f in last_file_state and t > last_file_state[f]
            }

            result_lines = []
            if new_files:
                result_lines.append("📂 **Созданы файлы:**")
                result_lines.extend(f"• `{f.name}`" for f in sorted(new_files, key=lambda x: x.name))
            if changed_files:
                result_lines.append("\n✏️ **Изменены файлы:**")
                result_lines.extend(f"• `{f.name}`" for f in sorted(changed_files, key=lambda x: x.name))

            if result_lines:
                await message.answer("\n".join(result_lines))

            # Полный ответ qwen
            qwen_response = parser.get_full_response()
            if qwen_response and qwen_response.strip():
                # Убираем префиксы stream-json для чистого ответа
                lines = qwen_response.split("\n")
                clean_lines = []
                for l in lines:
                    # Убираем эмодзи-префиксы stream событий
                    cleaned = l
                    for prefix in ("⚙️ ", "🧠 ", "💬 ", "🔧 ", "🏁 "):
                        if cleaned.startswith(prefix):
                            cleaned = cleaned[len(prefix):]
                            break
                    if cleaned.strip():
                        clean_lines.append(cleaned)

                if clean_lines:
                    full_text = "\n".join(clean_lines)
                    await _send_long_message(message, f"📋 Ответ:\n\n{full_text}")
                elif not result_lines:
                    await message.answer("✅ Задача выполнена.")
            elif not result_lines:
                await message.answer("✅ Задача выполнена.")

            # Очищаем буфер для следующей задачи
            parser.clear_full_buffer()
        else:
            await message.answer(f"⚠️ qwen завершился с кодом {returncode}")

    # --- Access check ---
    async def check_access(message: Message):
        if str(message.from_user.id) not in [str(uid) for uid in settings.ALLOWED_USER_IDS]:
            await message.answer("⛔ Доступ запрещён.")
            return False
        return True

    # --- Handlers ---
    @router.message(CommandStart())
    async def cmd_start(message: Message):
        if not await check_access(message):
            return

        if manager.is_running:
            await message.answer("🤖 Агент уже работает.")
            return

        await manager.start()
        await message.answer(
            "🤖 **Qwen Remote Control v3**\n\n"
            "**Задачи:**\n"
            "• Отправьте текст — qwen выполнит задачу\n\n"
            "**Файлы:**\n"
            "• `/ls [путь]` — содержимое директории\n"
            "• `/cat <файл>` — содержимое файла\n"
            "• `/tree [путь]` — дерево файлов\n"
            "• `/cd <путь>` — сменить директорию\n"
            "• `/pwd` — текущая директория\n\n"
            "**Проекты:**\n"
            "• `/project <имя> <путь>` — сохранить проект\n"
            "• `/load <имя>` — загрузить проект\n"
            "• `/projects` — список проектов\n\n"
            "**Другое:**\n"
            "• `/status` — статус\n"
            "• `/btw <вопрос>` — вопрос агенту\n"
            "• `/clear` — сбросить сессию"
        )

    # --- File commands ---
    @router.message(Command("ls"))
    async def cmd_ls(message: Message):
        if not await check_access(message):
            return
        args = message.text.replace("/ls", "").strip()
        result = file_mgr.list_dir(args if args else None)
        await message.answer(result)

    @router.message(Command("cat"))
    async def cmd_cat(message: Message):
        if not await check_access(message):
            return
        path = message.text.replace("/cat", "").strip()
        if not path:
            await message.answer("📝 Использование: `/cat <путь_к_файлу>`")
            return
        result = file_mgr.read_file(path)
        await message.answer(result)

    @router.message(Command("tree"))
    async def cmd_tree(message: Message):
        if not await check_access(message):
            return
        args = message.text.replace("/tree", "").strip()
        result = file_mgr.tree(args if args else None)
        if len(result) > 4000:
            result = result[:4000] + "\n... (обрезано)"
        await message.answer(result)

    @router.message(Command("cd"))
    async def cmd_cd(message: Message):
        if not await check_access(message):
            return
        path = message.text.replace("/cd", "").strip()
        if not path:
            await message.answer("📝 Использование: `/cd <путь>`")
            return
        result = file_mgr.set_working_dir(path)
        await message.answer(result)

    @router.message(Command("pwd"))
    async def cmd_pwd(message: Message):
        if not await check_access(message):
            return
        await message.answer(f"📂 `{file_mgr.get_working_dir()}`")

    # --- Project commands ---
    @router.message(Command("project"))
    async def cmd_project(message: Message):
        if not await check_access(message):
            return
        parts = message.text.replace("/project", "").strip().split(None, 1)
        if len(parts) < 2:
            await message.answer("📝 Использование: `/project <имя> <путь>`")
            return
        name, path = parts
        result = file_mgr.set_project(name, path)
        await message.answer(result)

    @router.message(Command("load"))
    async def cmd_load(message: Message):
        if not await check_access(message):
            return
        name = message.text.replace("/load", "").strip()
        if not name:
            await message.answer("📝 Использование: `/load <имя_проекта>`")
            return
        result = file_mgr.load_project(name)
        await message.answer(result)

    @router.message(Command("projects"))
    async def cmd_projects(message: Message):
        if not await check_access(message):
            return
        await message.answer(file_mgr.list_projects())

    # --- Status / clear ---
    @router.message(Command("status"))
    async def cmd_status(message: Message):
        if not await check_access(message):
            return
        text = parser.get_status_text() + "\n\n" + file_mgr.get_project_context()
        await message.answer(text)

    @router.message(Command("clear"))
    async def cmd_clear(message: Message):
        if not await check_access(message):
            return
        await manager.stop()
        manager._running = True
        await message.answer("🗑 Сессия сброшена.")

    @router.message(Command("btw"))
    async def cmd_btw(message: Message):
        if not await check_access(message):
            return
        question = message.text.replace("/btw", "").strip()
        if not question:
            await message.answer("📝 Использование: `/btw Ваш вопрос`")
            return

        pos = parser.mark_position()
        status_msg = await message.answer(f"❓ Спрашиваю: {question}")

        try:
            await manager.send_command(question, cwd=file_mgr.get_working_dir())

            response = parser.get_new_output(pos)
            if response and response.strip():
                lines = [l.strip() for l in response.split("\n") if l.strip()]
                meaningful = [
                    l for l in lines
                    if not any(l.startswith(p) for p in ("─", "│", "┌", "└", "┐", "┘",
                                                         "Tips:", "esc esc", "ctrl+", "shift+",
                                                         "Qwen OAuth"))
                ]
                answer_lines = []
                found_question = False
                for l in meaningful:
                    if question[:20].lower() in l.lower():
                        found_question = True
                        continue
                    if found_question and l.strip():
                        answer_lines.append(l)

                if answer_lines:
                    text = "\n".join(answer_lines)
                    if len(text) > 4000:
                        text = text[:4000] + "\n... (обрезано)"
                    await message.answer(f"💬 Ответ:\n\n{text}")
                else:
                    text = "\n".join(meaningful[-30:])
                    if len(text) > 4000:
                        text = text[:4000]
                    await message.answer(f"💬 Ответ:\n\n{text}")
            else:
                await message.answer("⚠️ Не удалось получить ответ.")
        except Exception as e:
            log.error(f"Ошибка /btw: {e}", exc_info=True)
            await message.answer(f"❌ Ошибка: {e}")

    # --- Task handler ---
    @router.message()
    async def handle_task(message: Message):
        if not await check_access(message):
            return

        status_msg = await message.answer("⏳ Запускаю qwen...")
        await _run_task(message.text, message, status_msg)

    return bot, dp
