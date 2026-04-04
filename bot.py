import asyncio
import logging
from pathlib import Path
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.enums import ParseMode
from process_manager import QwenProcessManager
from parser import LogParser, ActionCategory
from file_manager import FileManager
from config import settings

log = logging.getLogger(__name__)
router = Router()


def create_bot_and_setup(manager: QwenProcessManager, parser: LogParser, file_mgr: FileManager):
    bot = Bot(token=settings.TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher()

    # --- File change tracking ---
    last_file_state: dict[str, float] = {}

    def _scan_dir_sync(directory: Path) -> dict[str, float]:
        """Сканирует директорию (синхронно)."""
        result = {}
        try:
            for f in directory.rglob("*"):
                if f.is_file() and not any(part.startswith(".") for part in f.parts):
                    try:
                        result[str(f)] = f.stat().st_mtime
                    except OSError:
                        pass
        except PermissionError:
            pass
        return result

    async def _scan_dir(directory: Path) -> dict[str, float]:
        """Асинхронное сканирование директории."""
        return await asyncio.get_event_loop().run_in_executor(
            None, _scan_dir_sync, directory
        )

    async def _send_long_message(message: Message, text: str):
        """Отправляет длинное сообщение, разбивая на чанки по 4000 символов."""
        max_len = 4000
        sent_messages = []
        while text:
            chunk = text[:max_len]
            # Не режем посреди строки
            if len(text) > max_len:
                last_newline = chunk.rfind("\n")
                if last_newline > max_len * 0.7:
                    chunk = chunk[:last_newline]
            # Если обрезаем HTML-тег — закрываем его
            open_tags = chunk.count("<") - chunk.count("</")
            if open_tags > 0:
                chunk += "</pre>" * max(0, chunk.count("<pre>") - chunk.count("</pre>"))

            sent_msg = await message.answer(chunk)
            sent_messages.append(sent_msg)
            text = text[len(chunk):]
            if text:
                await asyncio.sleep(0.3)
        return sent_messages

    def _format_task_result(
        new_files: list[str] = None,
        changed_files: list[str] = None,
        qwen_text: str = None,
    ) -> str:
        """Форматирует финальный результат задачи."""
        parts = []

        if new_files:
            parts.append("<b>📂 Созданы файлы:</b>")
            for f in new_files:
                parts.append(f"• <code>{Path(f).name}</code>")
            parts.append("")

        if changed_files:
            parts.append("<b>✏️ Изменены файлы:</b>")
            for f in changed_files:
                parts.append(f"• <code>{Path(f).name}</code>")
            parts.append("")

        if qwen_text and qwen_text.strip():
            escaped = parser._escape_html(qwen_text)
            parts.append(f"<b>📋 Ответ агента:</b>\n\n<pre>{escaped}</pre>")

        return "\n".join(parts)

    async def _run_task(prompt: str, message: Message, status_msg: Message):
        """Запускает задачу qwen со стримингом в Telegram."""
        work_dir = file_mgr.get_working_dir()

        # Начальное состояние файлов (асинхронно!)
        nonlocal last_file_state
        last_file_state = await _scan_dir(work_dir)

        # Thread-safe буфер для стриминга — asyncio.Queue вместо list
        stream_queue: asyncio.Queue[str] = asyncio.Queue()
        task_done = asyncio.Event()
        returncode_holder = [0]

        def on_stream_line(line: str):
            """Колбэк — кладёт в queue, thread-safe."""
            actionable = parser.get_actionable_line(line)
            if actionable:
                # Отправляем в event loop
                try:
                    stream_queue.put_nowait(actionable)
                except Exception:
                    pass

        manager.set_stream_callback(on_stream_line)
        manager.set_final_result_callback(lambda text: parser.set_final_result(text))

        async def update_status_loop():
            """Обновляет статус-сообщение каждые 3 секунды."""
            while not task_done.is_set():
                await asyncio.sleep(3)

                # Строим прогресс-бар из parser
                progress = parser.get_progress_summary()
                if not progress:
                    continue

                try:
                    await status_msg.edit_text(
                        f"⏳ Выполняю:\n\n{progress}",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

        async def run_qwen():
            """Запускает qwen."""
            try:
                rc = await manager.send_command(prompt, cwd=work_dir)
                returncode_holder[0] = rc
            except Exception as e:
                log.error(f"Ошибка запуска: {e}", exc_info=True)
                returncode_holder[0] = -1
            finally:
                task_done.set()

        # Запускаем параллельно
        qwen_task = asyncio.create_task(run_qwen())
        status_task = asyncio.create_task(update_status_loop())

        await task_done

        # Финальное обновление
        await asyncio.sleep(0.5)
        manager.set_stream_callback(None)
        manager.set_final_result_callback(None)

        status_task.cancel()
        try:
            await status_task
        except asyncio.CancelledError:
            pass

        # Результат
        returncode = returncode_holder[0]
        if returncode == 0:
            # Файловые изменения
            current = await _scan_dir(work_dir)
            new_files = [f for f in current.keys() if f not in last_file_state]
            changed_files = [
                f for f in current.keys()
                if f in last_file_state and current[f] > last_file_state[f]
            ]

            # Формируем финальный ответ
            qwen_response = parser.get_full_response()
            result_html = _format_task_result(
                new_files=new_files if new_files else None,
                changed_files=changed_files if changed_files else None,
                qwen_text=qwen_response,
            )

            if result_html:
                await _send_long_message(message, result_html)
            else:
                await message.answer("✅ Задача выполнена.")

            # Inline-кнопки после задачи
            if new_files or changed_files:
                keyboard = _build_task_buttons(work_dir, new_files, changed_files)
                await message.answer("📎 Действия:", reply_markup=keyboard)

        else:
            await message.answer(f"⚠️ qwen завершился с кодом {returncode}")

        # Очистка
        parser.clear_full_buffer()

    def _build_task_buttons(
        work_dir: Path,
        new_files: list[str],
        changed_files: list[str],
    ) -> InlineKeyboardMarkup:
        """Inline-кнопки после выполнения задачи."""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[])

        # Кнопки для первых 3 файлов
        all_files = new_files + changed_files
        for fp in all_files[:3]:
            fname = Path(fp).name
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"📄 {fname}",
                    callback_data=f"view_file:{fp}",
                )
            ])

        # Кнопка повтора
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text="🔄 Повторить", callback_data="repeat_task"),
        ])

        return keyboard

    async def _handle_file_view(callback_query, file_path: str):
        """Обработка просмотра файла через inline-кнопку."""
        fp = Path(file_path)
        if not fp.exists() or not fp.is_file():
            await callback_query.answer("Файл не найден", show_alert=True)
            return

        if fp.stat().st_size > 50_000:
            # Отправляем как документ
            doc = FSInputFile(fp, filename=fp.name)
            await callback_query.message.answer_document(doc)
            await callback_query.answer()
        else:
            # Отправляем как текст
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
                escaped = parser._escape_html(content)
                text = f"<b>{fp.name}</b>:\n\n<pre>{escaped}</pre>"
                if len(text) > 4000:
                    text = text[:4000] + "\n...</pre>"
                await callback_query.message.answer(text, parse_mode=ParseMode.HTML)
            except Exception as e:
                await callback_query.answer(f"Ошибка: {e}", show_alert=True)

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
            await message.answer("🤖 Агент уже работает. Отправляйте задачи!")
            return

        await manager.start()
        await message.answer(
            "🤖 <b>Qwen Remote Control v3</b>\n\n"
            "Отправьте текст — qwen выполнит задачу.\n\n"
            "Используйте /help для списка команд."
        )

    @router.message(Command("help"))
    async def cmd_help(message: Message):
        if not await check_access(message):
            return
        await message.answer(
            "📋 <b>Доступные команды:</b>\n\n"
            "<b>Файлы:</b>\n"
            "/ls [путь] — содержимое директории\n"
            "/cat &lt;файл&gt; — содержимое файла\n"
            "/tree [путь] — дерево файлов\n"
            "/cd &lt;путь&gt; — сменить директорию\n"
            "/pwd — текущая директория\n\n"
            "<b>Проекты:</b>\n"
            "/project &lt;имя&gt; &lt;путь&gt; — сохранить проект\n"
            "/load &lt;имя&gt; — загрузить проект\n"
            "/projects — список проектов\n\n"
            "<b>Управление:</b>\n"
            "/status — статус и прогресс\n"
            "/cancel — отмена текущей задачи\n"
            "/btw &lt;вопрос&gt; — вопрос агенту\n"
            "/clear — сбросить сессию\n"
            "/help — эта справка"
        )

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message):
        if not await check_access(message):
            return
        await manager.stop()
        manager._running = True
        await message.answer("🛑 Задача отменена.")

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
            await message.answer("📝 Использование: <code>/cat &lt;путь_к_файлу&gt;</code>")
            return

        target = Path(path)
        if not target.is_absolute():
            target = file_mgr.get_working_dir() / target
        target = target.resolve()

        if not target.exists():
            await message.answer(f"❌ Файл не найден: <code>{target.name}</code>")
            return

        if target.stat().st_size > 50_000:
            # Отправляем как документ
            doc = FSInputFile(target, filename=target.name)
            await message.answer_document(doc)
        else:
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
            await message.answer("📝 Использование: <code>/cd &lt;путь&gt;</code>")
            return
        result = file_mgr.set_working_dir(path)
        await message.answer(result)

    @router.message(Command("pwd"))
    async def cmd_pwd(message: Message):
        if not await check_access(message):
            return
        await message.answer(f"📂 <code>{file_mgr.get_working_dir()}</code>")

    # --- Project commands ---
    @router.message(Command("project"))
    async def cmd_project(message: Message):
        if not await check_access(message):
            return
        parts = message.text.replace("/project", "").strip().split(None, 1)
        if len(parts) < 2:
            await message.answer("📝 Использование: <code>/project &lt;имя&gt; &lt;путь&gt;</code>")
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
            await message.answer("📝 Использование: <code>/load &lt;имя_проекта&gt;</code>")
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
            await message.answer("📝 Использование: <code>/btw Ваш вопрос</code>")
            return

        status_msg = await message.answer(f"❓ Спрашиваю: <i>{question}</i>")

        try:
            await manager.send_command(question, cwd=file_mgr.get_working_dir())

            # Используем final_result — он уже содержит полный ответ
            response = parser.final_result
            if not response:
                response = parser.get_full_response()

            if response and response.strip():
                escaped = parser._escape_html(response)
                # Обрезаем если слишком длинный
                if len(escaped) > 4000:
                    escaped = escaped[:4000] + "..."
                await message.answer(
                    f"<b>💬 Ответ:</b>\n\n<pre>{escaped}</pre>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await message.answer("⚠️ Не удалось получить ответ.")
        except Exception as e:
            log.error(f"Ошибка /btw: {e}", exc_info=True)
            await message.answer(f"❌ Ошибка: {e}")

    # --- Callback queries (inline buttons) ---
    @router.callback_query(lambda c: c.data and c.data.startswith("view_file:"))
    async def callback_view_file(callback_query):
        file_path = callback_query.data.split(":", 1)[1]
        await _handle_file_view(callback_query, file_path)

    @router.callback_query(lambda c: c.data == "repeat_task")
    async def callback_repeat_task(callback_query):
        await callback_query.answer("Отправьте задачу снова!", show_alert=True)

    # --- Task handler ---
    @router.message()
    async def handle_task(message: Message):
        if not await check_access(message):
            return

        status_msg = await message.answer("⏳ Запускаю qwen...")
        await _run_task(message.text, message, status_msg)

    return bot, dp
