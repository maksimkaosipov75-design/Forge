# OpenRouter API Architecture for Forge

Статус: draft

Этот документ фиксирует рекомендацию по внедрению API-моделей в Forge без поломки текущей CLI-first архитектуры.

## 1. Короткий ответ

Да, OpenRouter в проект добавлять стоит.

Но правильный путь для Forge:

- не заменять CLI-провайдеры сразу
- добавить второй execution backend для API-моделей
- использовать API-провайдеры там, где они реально сильнее: planner, review, synthesis, fallback, read-only analysis
- оставлять CLI-провайдеры основным механизмом для write-heavy agentic execution

Итоговая рекомендация:

- `CLI agents` остаются ядром исполнения
- `API models` становятся новым стратегическим слоем гибкости

## 2. Почему не стоит просто “уйти от CLI”

На текущий момент Forge уже построен вокруг запуска внешних agent CLI:

- `qwen`
- `codex`
- `claude`

Их сильная сторона не только в модели, но и в готовом агентном рантайме:

- обработка tool use
- работа с файлами
- shell-команды
- потоковый вывод
- встроенное поведение сессии и resume

Если просто заменить всё на API-вызовы, проект потеряет часть готовой агентности, и эту логику придётся реализовывать внутри Forge.

Поэтому “OpenRouter вместо CLI” как резкая замена сейчас выглядит рискованно.

## 3. Что OpenRouter реально улучшит

OpenRouter как API-слой даёт Forge несколько сильных преимуществ:

- быстрое подключение новых моделей без отдельного CLI-интегратора
- доступ к free и low-cost моделям
- единый OpenAI-compatible HTTP интерфейс
- проще управлять fallback, routing и cost-aware выбором модели
- проще делать planner/reviewer/synthesis без зависимости от локальных установок
- проще собирать telemetry, usage и latency на уровне самого Forge

Это архитектурно полезно даже если CLI-провайдеры никуда не исчезнут.

## 4. Рекомендованная целевая архитектура

Нужно уйти от мысли “provider = subprocess CLI”.

Вместо этого рекомендую такую модель:

### A. Provider definition

Каждый provider описывается метаданными:

- `name`
- `label`
- `transport`
- `capabilities`
- `default_model`
- `available_models`
- `strengths`

Где `transport` это:

- `cli`
- `api`

### B. Execution backend

Добавить два типа backend:

- `CliExecutionBackend`
- `ApiExecutionBackend`

Они должны скрывать детали транспорта, но реализовывать одинаковый контракт выполнения.

### C. Unified runtime contract

Forge нужен общий runtime contract, например такого типа:

- `start_session()`
- `run(prompt, cwd, model, stream_handler, final_handler)`
- `cancel()`
- `health()`
- `supports(capability)`

Главное здесь то, что orchestration service и UI больше не должны зависеть от того, subprocess это или HTTP API.

## 5. Как я бы разделил провайдеры

## CLI providers

Оставляем как есть:

- `qwen`
- `codex`
- `claude`

Назначение:

- полноценные coding runs
- file edits
- shell-heavy tasks
- сложные multi-step write tasks

## API providers

Добавляем новый тип:

- `openrouter`

Назначение:

- planning
- re-planning
- review
- synthesis
- summarization
- read-only analysis
- дешёвый fallback

Позже можно добавить и другие API-провайдеры, но `openrouter` логично сделать первым.

## 6. Почему именно один provider openrouter, а не десяток новых providers

Неправильный вариант:

- `nemotron`
- `minimax`
- `deepseek`
- `qwen-api`
- `llama-api`

Каждый как отдельный provider.

Почему это плохо:

- засоряет provider registry
- усложняет UI
- размазывает конфиг
- усложняет routing

Правильный вариант:

- один provider `openrouter`
- внутри него набор моделей
- роутинг идёт по `provider + model`

То есть пользователь выбирает не “нового провайдера”, а “новую модель внутри API-провайдера”.

## 7. Предлагаемые capabilities

У каждого provider/backend должны быть capabilities, чтобы orchestration принимал решения не по имени провайдера, а по возможностям.

Минимальный набор:

- `streaming`
- `session_resume`
- `file_editing`
- `shell_execution`
- `tool_use`
- `structured_output`
- `long_context`
- `low_cost`
- `planner`
- `reviewer`
- `synthesis`

Тогда orchestration сможет мыслить так:

- нужен `planner + low_cost + structured_output`
- нужен `file_editing + shell_execution`
- нужен `reviewer + long_context`

Это сильнее и масштабируемее, чем текущая логика “claude лучше для UI, codex лучше для backend”.

## 8. Где OpenRouter приносит максимум пользы

Я бы внедрял его в таком порядке:

### Phase 1. Planner and synthesis only

Использовать OpenRouter для:

- AI planner
- re-plan
- synthesis финального ответа
- optional review

Почему это лучший старт:

- минимальный риск
- быстрый выигрыш в гибкости
- не ломает write path

### Phase 2. Read-only orchestration tasks

Использовать OpenRouter для:

- repository analysis
- summarization
- diff explanation
- issue drafting
- plan generation

### Phase 3. Experimental agentic tasks

Если захочется, можно позже добавить:

- API-модели в простые coding subtasks
- но только через ограниченный встроенный tool layer

Этот этап я не считаю обязательным для `0.2.x`.

## 9. Что нужно изменить в коде

Ниже не финальная схема имён, а рекомендуемое направление.

### 1. `providers.py`

Сейчас там только статический список CLI-провайдеров.

Нужно:

- добавить тип транспорта
- добавить capabilities
- добавить default model
- добавить модельный каталог для API-провайдеров

Примерно:

- `ProviderDefinition`
- `ModelDefinition`
- `ProviderTransport`

### 2. `config.py`

Добавить настройки:

- `OPENROUTER_API_KEY`
- `OPENROUTER_BASE_URL`
- `OPENROUTER_DEFAULT_MODEL`
- возможно `OPENROUTER_HTTP_TIMEOUT`

### 3. Новый модуль API-клиента

Например:

- `api_client.py`
- или `runtime/api_provider.py`

Он должен:

- делать chat/completions запросы
- поддерживать streaming
- нормализовать ошибки
- собирать usage/token metrics

### 4. Новый backend/manager слой

Сейчас основной abstraction это process manager.

Нужно ввести более общий слой, например:

- `BaseExecutionBackend`
- `CliExecutionBackend`
- `OpenRouterExecutionBackend`

Или мягкий переход:

- оставить `ProcessManager` для CLI
- добавить рядом `ApiModelManager`

Я бы выбрал первый вариант, потому что он чище архитектурно.

### 5. `runtime/container.py`

Нужно, чтобы container создавал runtime не только для CLI-провайдера, но и для API-провайдера.

Нужно уметь:

- резолвить backend по transport type
- подставлять model config
- учитывать API provider в planner routing

### 6. `task_models.py`

Нужно проверить и, возможно, расширить:

- модель provider/model metadata в `TaskResult`
- usage fields для prompt/output tokens
- transport type
- cost estimate

Это важно для observability.

### 7. `orchestrator_service.py`

Нужно:

- научить orchestration использовать capabilities
- разрешить routing на API-провайдеры только там, где это безопасно
- не выбирать API-модель для write-heavy subtasks без явного разрешения

### 8. UI and commands

Понадобятся команды или расширение текущих:

- `/providers`
- `/model`
- `/model openrouter minimax/minimax-m2.5:free`
- `/status`
- `/limits`

Пользователь должен видеть не только provider, но и текущую модель.

## 10. Какой контракт я бы предложил

Я бы ввёл что-то вроде:

### Provider runtime request

- provider
- model
- prompt
- cwd
- session_id
- mode
- capabilities_required

### Provider runtime result

- provider
- model
- transport
- final_text
- status
- failure_reason
- usage
- duration_ms
- raw_events

Это поможет выровнять CLI и API исполнения в одну модель.

## 11. Какие модели стоит поддержать первыми

Для `openrouter` я бы не делал огромный каталог.

Стартовый allowlist:

- `qwen/qwen3-coder:free`
- `minimax/minimax-m2.5:free`
- `openrouter/free`

Опционально:

- один `nemotron` как experimental reasoning model

Почему так:

- маленький стартовый список проще тестировать
- проще описать пользователю, что и для чего подходит
- меньше риск, что продукт превратится в “каталог моделей без стратегии”

## 12. Как я бы использовал эти модели

### `qwen/qwen3-coder:free`

Использование:

- code analysis
- lightweight coding
- plan generation для технических задач

### `minimax/minimax-m2.5:free`

Использование:

- synthesis
- planning
- review
- mixed prompts

### `openrouter/free`

Использование:

- только ручной fallback
- только non-critical задачи

Не стоит делать его default route.

## 13. Чего делать не надо

Я бы не рекомендовал:

- переписывать весь runtime вокруг HTTP уже в `0.2.0`
- убирать текущие CLI-провайдеры
- запускать API-модели в file-editing path без отдельного tool execution слоя
- поддерживать десятки моделей с первого дня
- смешивать “provider identity” и “model identity”

## 14. Рекомендованный rollout

## Stage 1. Foundation

- расширить provider definitions
- добавить config для OpenRouter
- добавить OpenRouter API client
- добавить transport-aware runtime contract

Результат:

- Forge умеет обращаться к API-модели как к нормальному provider runtime

## Stage 2. Safe integration

- подключить OpenRouter только в planner/review/synthesis path
- показать usage/model info в UI
- собрать telemetry и failure stats

Результат:

- API-провайдер приносит реальную пользу без риска для core execution

## Stage 3. Optional expansion

- read-only analysis flows
- cheap fallback
- experiments с lightweight agentic API tasks

## 15. Моё итоговое решение

Если выбирать стратегию для Forge, я рекомендую:

- не “CLI или API”
- а “CLI для агентного исполнения, API для гибкости и масштабирования”

В практическом виде это значит:

- добавить `openrouter` как новый API-провайдер
- не ломать существующие `qwen/codex/claude`
- использовать OpenRouter сначала в planner/review/synthesis
- только после этого решать, стоит ли развивать API-agent слой глубже

Это даст проекту:

- более современную архитектуру
- более простой путь к новым моделям
- лучшее управление стоимостью и fallback
- меньше зависимости от внешних CLI

Но без потери уже работающего agentic execution.
