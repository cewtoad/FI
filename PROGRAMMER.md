# Programmer notes (FI)

Short map for bugfixes and small features. Prefer the smallest change that
matches existing seams; do not invent parallel frameworks.

## Run tests

```bash
py -3.12 -m pytest -q
# focused examples:
py -3.12 -m pytest -q tests/test_p0_stable_radio.py tests/test_web_settings.py
py -3.12 -m pytest -q tests/test_prompt_overlay.py tests/test_qa_enhancements.py
```

Offline suite is the default gate; avoid claiming hardware results without a device.

## Assembly (composition root)

- **`app.build_app(...)`** — single place that wires receiver, state, summariser,
  engineer, race model, radio director, speech, recorder, ticker.
- **Entrypoints that must use it:** `webui.serve`, `voice_main`, console `run.py`
  (after P0). If a new mode needs the radio stack, call `build_app`; do not
  hand-assemble a second graph.
- **Extras bag:** `app.extras` holds optional pieces (`voice`, alert log, …).

## Config / settings

| Concern | Where |
|---|---|
| Declare a new tunable | **`config_schema.py`** (`Setting(...)` + group). One line; `RUNTIME_KEYS` is derived. |
| Persist / hot-reload | `config.Config.set_runtime(..., persist=True)` |
| Web batch apply | `config_ui.apply_settings` (used by both webui and `--config`) |
| Primary UI (live) | Web panel **全部设置 / All settings** on port **8765** — schema-driven, includes PTT capture (kb/HID/Xbox), audio, voice packs |
| Same-port twin page | **`GET /settings`** on 8765 (serves `config_ui` page) |
| Offline twin | `FI.py --config` → port **8766** (`config_ui.serve`) |

Adding a setting: edit schema → it appears in both UIs automatically. No need to
hard-code HTML rows.

### Custom prompts

- Built-in: `prompts.SYSTEM_PROMPT`
- Optional one-line: `CUSTOM_SYSTEM_PROMPT` (.env / schema)
- Optional multi-line file: `custom_system_prompt.txt` next to `.env` (`app_root()`)
- UI: web **全部设置 → 自定义 AI 提示词**, or `GET/POST /api/prompt_overlay`
- **`prompts.SAFETY_LINE`** is always appended last (“advise only, never press
  keys”) and cannot be removed by overlays.

## Ask path locks (after P0)

- Process-wide **`webui._ASK_SEMAPHORE`** (capacity 1) is acquired by both
  `/api/ask` and `/api/ask_voice` so text and voice cannot run together.
- **`Engineer._ask_lock`** serialises `ask()` / history mutations for any caller
  (web text, web voice, in-game voice).
- Do not add a third ask entrypoint without taking the same semaphore (web) and
  going through `Engineer.ask`.

## Packaging (existing)

Windows release zips are built by **`build_release.ps1`** (PyInstaller core +
optional full embedded voice pack). Manifest args come from
`build_manifest.py` so CI and local builds stay aligned. See README “下载与安装”
and `RELEASE_SIGNING.md`. Do not invent a second packaging path unless the
existing script cannot cover the need.

## Non-goals (do not implement)

- Any key/gamepad/steering injection (`SendInput`, virtual pads, HID write,
  firmware poking). Radio may **advise** only.
- Memory reading / anti-cheat bypass.
