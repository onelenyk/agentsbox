# AI Runner CRUD — управління AI-раннерами

## TL;DR

> **Quick Summary**: Додати повноцінне управління AI-раннерами (opencode, Claude CLI, локальні моделі тощо) через Web UI, CLI та API. Раннер — це конфігурація AI-бекенду: шлях до бінарника, API ключ, endpoint, модель за замовчуванням. Кожен агент може використовувати свій раннер. Заміна хардкоду `"opencode"` у `launcher.py` на динамічний вибір раннера.
>
> **Deliverables**:
> - `core/runners.py` — CRUD операції для раннерів (SQLite + файли)
> - `core/launcher.py` — рефакторинг: вибір раннера з БД/конфігу замість хардкоду
> - `web/templates/runners*.html` — UI для управління раннерами
> - `web/routes.py` — API ендпоінти для раннерів
> - `sisyphus.py` — CLI команди для раннерів
>
> **Estimated Effort**: Medium
> **Parallel Execution**: YES — 3 waves
> **Critical Path**: Task 1 → Task 2 → Task 4

---

## Поточний стан — що не так

### Як зараз працює launcher.py:
```python
# core/launcher.py:87-95 — ХАРДКОД
proc = await asyncio.create_subprocess_exec(
    "opencode",           # ← завжди opencode, не можна змінити
    "-p", prompt,
    "-c", str(worktree_path),
    "-f", "json",
    ...
)
```

### Як зараз працюють агенти:
```yaml
# agents/android-engineer.yaml
name: "Android Engineer"
model: "deepseek"              # ← модель вказана, але раннера немає
opencode_config:               # ← opencode-специфічний конфіг
  tools: [bash, read, grep]
  timeout: 1800
```

**Проблеми:**
1. `launcher.py` хардкодить `"opencode"` — не можна використати інший раннер
2. API ключі (OpenRouter) лежать в `config.yaml` — не прив'язані до агентів
3. Немає способу додати новий раннер без зміни коду
4. Немає UI/CLI для управління раннерами
5. Немає health check для раннера (чи доступний API? чи валідний ключ?)

---

## Що таке "раннер"

**Раннер** = конфігурація AI-бекенду. Один запис у БД/конфізі:

```yaml
# Приклад: runners/opencode.yaml (або рядок у SQLite)
name: "OpenCode DeepSeek"
binary: "opencode"              # шлях до бінарника
type: "opencode"                # тип: opencode / claude-cli / openai-api / ollama
model: "deepseek/deepseek-v4"   # модель за замовчуванням
api_key_env: "OPENROUTER_API_KEY"  # звідки брати API ключ
base_url: "https://openrouter.ai/api/v1"
default: true                   # чи використовувати за замовчуванням
enabled: true
```

```yaml
# Приклад: runners/claude.yaml
name: "Claude via CLI"
binary: "claude"
type: "claude-cli"
model: "claude-sonnet-4-20250514"
api_key_env: "ANTHROPIC_API_KEY"
default: false
enabled: true
```

```yaml
# Приклад: runners/local-llama.yaml
name: "Local Llama 3"
binary: "ollama"
type: "ollama"
model: "llama3:70b"
base_url: "http://localhost:11434"
api_key_env: ""                 # локальна модель — без ключа
default: false
enabled: true
```

### Зв'язок агент ↔ раннер:

```yaml
# Оновлений agents/android-engineer.yaml
name: "Android Engineer"
runner: "opencode-deepseek"     # ← посилання на раннер (key/name)
model: "deepseek"               # ← модель (може перевизначати раннер)
skills: [gitlab-workflow]
```

### Як буде працювати launcher після змін:

```python
# Псевдокод
runner = runners.get(agent_def["runner"])  # отримати конфіг раннера
binary = runner["binary"]                  # "opencode" або "claude" або "ollama"
args = build_args(binary, runner, agent_def, prompt)
proc = await asyncio.create_subprocess_exec(*args)
```

---

## Робочі цілі

### Core Objective
Додати CRUD для AI-раннерів: створення, редагування, видалення, health check. Рефакторити `launcher.py` для динамічного вибору раннера. Додати UI (як для repos) і CLI команди.

### Concrete Deliverables
- `sisyphus/core/runners.py` — CRUD + health check (~150 lines)
- `sisyphus/core/launcher.py` — рефакторинг (~20 lines змін)
- `sisyphus/web/templates/runners.html` — список раннерів
- `sisyphus/web/templates/runners_new.html` — форма створення
- `sisyphus/web/templates/runners_edit.html` — форма редагування
- `sisyphus/web/templates/runners_list.html` — HTMX-фрагмент списку
- `sisyphus/web/routes.py` — 5 нових ендпоінтів
- `sisyphus/sisyphus.py` — 4 нові CLI команди
- `sisyphus/agents/` — оновлення YAML-ів (додати поле `runner`)

### Definition of Done
- [ ] `core/runners.py` — CRUD: load, list, add, update, delete, health_check
- [ ] `core/launcher.py` — приймає `runner_name`, використовує runners.get()
- [ ] Web UI: сторінка /runners з таблицею, формами, health-індикатором
- [ ] CLI: `sisyphus runners list`, `add`, `edit`, `delete`, `health`
- [ ] Агенти пов'язані з раннерами (поле `runner` в agent YAML)
- [ ] Health check працює (перевіряє доступність API)
- [ ] Всі 70 тестів проходять
- [ ] Міграція: старі агенти без поля `runner` працюють через default runner

### Must Have
- CRUD для раннерів (SQLite або YAML-файли)
- Динамічний вибір бінарника в launcher.py
- Web UI для управління (як repos)
- CLI команди
- Health check
- Default runner для зворотної сумісності

### Must NOT Have
- Не змінювати логіку pipeline.py
- Не ламати існуючі агенти (без поля `runner` — використовувати default)
- Не видаляти opencode_config з агентів (можуть знадобитися)

---

## Спосіб зберігання

### Варіант A: YAML-файли (як агенти)
```
sisyphus/runners/
├── opencode-deepseek.yaml
├── claude-sonnet.yaml
└── local-llama.yaml
```
Плюс: просто, git-friendly. Мінус: немає валідації, немає health history.

### Варіант B: SQLite (як repos)
```sql
CREATE TABLE runners (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    binary TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'opencode',
    model TEXT NOT NULL DEFAULT '',
    api_key_env TEXT DEFAULT '',
    base_url TEXT DEFAULT '',
    is_default INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    extra_args TEXT DEFAULT '[]',
    health_status TEXT DEFAULT 'unknown',
    health_checked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```
Плюс: валідація, health tracking, історія. Мінус: не git-friendly.

### Вибір: **Варіант B (SQLite)** — консистентно з repos, має health tracking.

---

## Execution Strategy

```
Wave 1 (Foundation):
├── Task 1: core/runners.py — CRUD + health check + DB schema
├── Task 2: core/launcher.py — рефакторинг (динамічний бінарник)

Wave 2 (UI, MAX PARALLEL):
├── Task 3: Web UI — runners templates + routes (як repos)
├── Task 4: CLI — runners команди

Wave 3 (Integration):
├── Task 5: Оновлення agent YAML-ів + default runner
├── Task 6: Тести + E2E верифікація
```

Critical Path: Task 1 → Task 2 → Task 5 → Task 6
Max Concurrent: 2 (Task 3 + Task 4 parallel)

---

## TODOs

- [ ] 1. Create `core/runners.py` — CRUD + health check + DB schema

  **Файл**: `sisyphus/core/runners.py`

  **Функції**:
  ```python
  def init_runners_table():        # CREATE TABLE IF NOT EXISTS runners
  def add_runner(name, binary, type, model, api_key_env, base_url, extra_args, is_default) -> dict
  def get_runner(runner_id) -> dict | None
  def get_runner_by_name(name) -> dict | None
  def get_default_runner() -> dict  # повертає runner з is_default=1
  def list_runners() -> list[dict]
  def update_runner(runner_id, **fields) -> dict
  def delete_runner(runner_id) -> None
  def set_default_runner(runner_id) -> None  # скидає всі is_default, ставить цьому
  async def health_check(runner_id) -> dict  # перевіряє доступність API
  ```

  **Health check**:
  - Перевіряє наявність бінарника (`shutil.which(binary)`)
  - Якщо є `api_key_env` — перевіряє що змінна встановлена
  - Якщо є `base_url` — робить HEAD/GET запит (перевірка доступності)
  - Повертає `{"status": "healthy"|"unhealthy", "error": "...", "checked_at": "..."}`
  - Оновлює `health_status` і `health_checked_at` в БД

  **Auto-seed**: при першому запуску (якщо таблиця порожня) створити default runner з поточними налаштуваннями з config.yaml:
  ```python
  add_runner(
      name="opencode-default",
      binary="opencode",
      type="opencode",
      model=get_config("default_model", "deepseek"),
      api_key_env="OPENROUTER_API_KEY",
      base_url=get_config("openrouter.base_url", ""),
      is_default=True,
  )
  ```

  **Must NOT**: не змінювати інші core/ модулі.

  **Agent**: `deep` | Skills: []

- [ ] 2. Refactor `core/launcher.py` — динамічний вибір раннера

  **Що змінити**:

  1. Функція `launch_agent()` приймає новий параметр `runner_name: str = None`
  2. Якщо `runner_name` передано → `runners.get_runner_by_name(runner_name)`
  3. Якщо ні → `runners.get_default_runner()`
  4. Якщо раннера немає → fallback на хардкод `"opencode"` (зворотна сумісність)
  5. Бінарник береться з `runner["binary"]` замість хардкоду
  6. API ключ: `runner["api_key_env"]` → `os.environ.get(...)` → додати в `env`
  7. `base_url`: якщо є → додати в `env` як `OPENROUTER_BASE_URL` / `ANTHROPIC_BASE_URL`
  8. `extra_args`: список додаткових аргументів командного рядка

  **Псевдокод**:
  ```python
  async def launch_agent(agent_def, prompt, worktree_path, output_dir, timeout=300, cancel_event=None, runner_name=None):
      # Отримати конфіг раннера
      if runner_name:
          runner = get_runner_by_name(runner_name)
      else:
          runner = get_default_runner()
      
      if not runner:
          runner = {"binary": "opencode"}  # fallback
      
      binary = runner["binary"]
      
      # Додати API ключ в env якщо потрібно
      if runner.get("api_key_env"):
          key = os.environ.get(runner["api_key_env"], "")
          if key:
              env["OPENROUTER_API_KEY"] = key  # або інший ключ залежно від типу
      
      # Сформувати аргументи
      extra = json.loads(runner.get("extra_args", "[]"))
      args = [binary] + extra + ["-p", prompt, "-c", str(worktree_path), "-f", "json"]
      
      proc = await asyncio.create_subprocess_exec(*args, ...)
  ```

  **Must NOT**: не змінювати логіку таймаутів/cancel/валідації.

  **Agent**: `deep` | Skills: []

- [ ] 3. Refactor `core/pipeline.py` — передавати runner_name

  **Що змінити**:
  - У `_run_step()`: прочитати `agent_def.get("runner")` → передати в `launch_agent(runner_name=...)`
  - У `_rerun_engineer()`: те саме

  **Agent**: `deep` | Skills: []

- [ ] 4. Web UI — runners templates + routes

  **Templates** (4 нових файли, за прикладом repos):
  ```
  web/templates/runners.html        ← сторінка /runners
  web/templates/runners_list.html   ← HTMX-фрагмент з таблицею
  web/templates/runners_new.html    ← форма створення
  web/templates/runners_edit.html   ← форма редагування
  ```

  **Routes** (5 нових ендпоінтів):
  ```
  GET  /runners              ← сторінка зі списком
  GET  /api/runners/list     ← HTMX-фрагмент таблиці
  GET  /api/runners/new-form ← форма створення
  POST /api/runners          ← створити раннер
  GET  /api/runners/{id}/edit-form ← форма редагування
  POST /api/runners/{id}/edit      ← оновити раннер
  POST /api/runners/{id}/delete    ← видалити раннер
  POST /api/runners/{id}/set-default ← зробити default
  POST /api/runners/{id}/health    ← запустити health check
  ```

  **Таблиця в UI**:
  | Name | Binary | Type | Model | Health | Default | Actions |
  |---|---|---|---|---|---|---|
  | OpenCode DS | opencode | opencode | deepseek-v4 | 🟢 | ⭐ | Edit Delete |

  **Health індикатор**: 🟢 healthy / 🔴 unhealthy / ⬜ unknown.
  Оновлюється через `hx-post="/api/runners/{id}/health" hx-trigger="load"`.

  **Agent**: `visual-engineering` | Skills: [frontend-ui-ux]

- [ ] 5. CLI — runners команди

  ```bash
  sisyphus runners list              # показати всіх
  sisyphus runners add <name> ...    # створити
  sisyphus runners edit <id> ...     # редагувати
  sisyphus runners delete <id>       # видалити
  sisyphus runners set-default <id>  # зробити default
  sisyphus runners health [id]       # health check
  ```

  **Agent**: `deep` | Skills: []

- [ ] 6. Оновлення agent YAML-ів + міграція

  **Що зробити**:
  - Додати поле `runner: "opencode-default"` в кожен agent YAML
  - `agents/__init__.py`: load_agent() — якщо `runner` відсутнє → додати `"opencode-default"` автоматично
  - `core/runners.py` при init: якщо таблиця порожня → auto-seed default runner

  **Agent**: `quick` | Skills: []

- [ ] 7. Тести

  **Нові тести** (`sisyphus/tests/test_runners.py`):
  - `test_add_runner` — створити раннер, перевірити поля
  - `test_get_default_runner` — перевірити default
  - `test_set_default_runner` — змінити default
  - `test_health_check` — перевірити healthy/unhealthy
  - `test_launcher_with_runner` — мок-тест запуску з різними раннерами
  - `test_fallback_no_runner` — перевірити fallback на opencode

  **Оновлення існуючих тестів**: перевірити що launcher тести все ще проходять.

  **Agent**: `quick` | Skills: []

- [ ] 8. E2E верифікація

  - Запустити сервер, перевірити сторінку /runners
  - Створити раннер через UI, перевірити health
  - Створити картку, запустити — перевірити що використовується правильний раннер
  - CLI: `sisyphus runners list`, `add`, `health`
  - Всі 70+ тестів проходять

  **Agent**: `unspecified-high` | Skills: []

---

## Final Verification Wave

- [ ] F1. Plan Compliance — `oracle`
- [ ] F2. Code Quality — `unspecified-high`
- [ ] F3. Manual QA — `unspecified-high`
- [ ] F4. Scope Fidelity — `deep`

---

## Success Criteria

- [ ] `core/runners.py` з усіма CRUD функціями + health check
- [ ] `launcher.py` використовує раннер з БД замість хардкоду
- [ ] Web UI для /runners (як /repos)
- [ ] CLI команди `sisyphus runners *`
- [ ] Health check показує статус раннера
- [ ] Default runner для зворотної сумісності
- [ ] Всі тести проходять (70+)
- [ ] Жоден існуючий функціонал не зламаний
