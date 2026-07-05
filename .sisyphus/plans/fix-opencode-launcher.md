# Fix opencode Launcher Integration

## TL;DR

> **Quick Summary**: opencode `--prompt` flag runs in interactive-like mode and never exits. The correct command is `opencode run "message"` which is designed for single-prompt non-interactive execution. Fix launcher.py arguments and add file-watching for early exit detection.
>
> **Deliverables**:
> - `core/launcher.py` — исправить аргументы на `opencode run` + `--format json` + `--auto`
> - `core/launcher.py` — добавить file-watching для result.json (выходить как только файл создан, не ждать таймаут)
>
> **Estimated Effort**: Low (2 tasks)

---

## Проблема

### Что сейчас:
```python
# launcher.py — НЕПРАВИЛЬНО
args = ["opencode", "--prompt", prompt]
```

**Результат**: opencode выполняет промпт, пишет result.json и analysis.md, но **не выходит** — висит в интерактивном режиме бесконечно. Launcher ждёт `proc.communicate()` → таймаут 300 секунд.

### Что нужно:
```python
# launcher.py — ПРАВИЛЬНО
args = ["opencode", "run", prompt, "--format", "json", "--auto"]
```

`opencode run` — это специальная подкоманда для non-interactive выполнения. Она принимает сообщение как позиционный аргумент, выполняет его и **выходит**.

### Дополнительная оптимизация — file-watching

Даже с правильной командой, можно ускорить завершение: как только `result.json` появился в output_dir — можно слать SIGTERM, не дожидаясь полного выхода процесса. Это даст мгновенное завершение вместо ожидания.

---

## Execution Strategy

```
Wave 1:
├── Task 1: Fix launcher arguments (opencode → opencode run)
└── Task 2: Add file-watching for early exit
```

---

## TODOs

- [ ] 1. Fix launcher.py — использовать `opencode run`

  **Что изменить** в `core/launcher.py`:

  ```python
  # Было:
  args = [binary] + extra_args + [
      "--prompt", prompt,
  ]
  
  # Стало:
  args = [binary, "run"] + extra_args + [
      prompt,
      "--format", "json",
      "--auto",
  ]
  ```

  Добавить опционально:
  - `--dir, str(worktree_path)` — если worktree_path не /tmp
  - `--model, runner_model` — если runner задаёт модель
  - `--agent, agent_name` — если агент хочет использовать конкретного opencode-агента

  **Must NOT**: не менять логику таймаутов, cancel_event, валидации.

- [ ] 2. Add file-watching — early exit при появлении result.json

  **Что добавить** в `launch_agent()`:

  После запуска процесса, параллельно с `proc.communicate()`, запустить цикл проверки:
  ```python
  async def _watch_for_result(output_dir, proc, timeout):
      """Poll for result.json; kill process early when found."""
      result_path = output_dir / "result.json"
      deadline = time.monotonic() + timeout
      while time.monotonic() < deadline:
          if result_path.exists():
              proc.terminate()
              return True
          await asyncio.sleep(1)
      return False
  ```

  Интегрировать в основную логику:
  ```python
  communicate_task = asyncio.create_task(proc.communicate())
  watch_task = asyncio.create_task(_watch_for_result(output_path, proc, timeout))
  
  done, pending = await asyncio.wait(
      [communicate_task, watch_task],
      return_when=asyncio.FIRST_COMPLETED,
  )
  ```

  **Must NOT**: не менять cancel_event логику, не ломать существующие таймауты.

---

## Success Criteria

- [ ] `opencode run "list files" --format json --auto` завершается и выходит
- [ ] Launcher читает result.json после выхода процесса
- [ ] File-watching убивает процесс как только result.json готов (экономит время)
- [ ] Все 82 теста проходят
- [ ] Реальный пайплайн завершается за <30 секунд (вместо 300s таймаута)
