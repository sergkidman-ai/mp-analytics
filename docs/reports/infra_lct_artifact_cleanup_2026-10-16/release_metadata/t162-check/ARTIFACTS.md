# Артефакты для передачи

Комплект для офлайн-развёртывания (ответ организаторов О-1: «контейнеры, модели, инструкции, OpenAPI и пример JSON»).
Собирается скриптом `scripts/release_artifacts.sh <тег>` в каталог `<тег>/`; OpenAPI и пример JSON — внутри образа и в
репозитории (`docs/openapi.yaml`, `docs/examples/submission_example.json`).

## Что в каталоге

| Файл | Что это | Обязателен |
|---|---|---|
| `ai-inspector-cpu.tar.gz` | Docker-образ сервиса (цель `cpu`): API, экран, анализ, распознавание сканов с моделями внутри; работает без сети | да |
| `models-llm.tar.gz` | веса языковой модели Qwen2.5-7B-Instruct (GGUF q4_k_m) и сервер llama.cpp b11001 — профиль `llm` | нет |
| `models-htr.tar.gz` | веса модели рукописи TrOCR `raxtemur/trocr-base-ru` — профиль `htr` | нет |
| `docker-compose.yml`, `docker-compose.htr.yml` | запуск и профили | да |
| `ARTIFACTS.md` | эта инструкция | — |
| `LICENSE` | лицензия | — |
| `release.txt` | тег, коммит, дата сборки, идентификатор образа, ответ `/health` при проверке без сети | — |
| `SHA256SUMS` | контрольные суммы всех файлов | — |

Модели, версии, лицензии и контрольные суммы самих весов — `docs/MODELS.md` репозитория.

## 1. Проверить целостность

```bash
cd <каталог комплекта>
sha256sum -c SHA256SUMS
```

## 2. Загрузить образ

```bash
gunzip -c ai-inspector-cpu.tar.gz | docker load          # → ai-inspector:<тег>
docker tag ai-inspector:<тег> ai-inspector:cpu            # имя, которое ждёт docker-compose.yml
```

## 3. Распаковать веса (необязательно)

Без весов сервис работает полностью; веса нужны только для подсказки ИИ (профиль `llm`) и чтения рукописных отметок
(профиль `htr`).

```bash
mkdir -p models
tar -xzf models-llm.tar.gz -C models     # → models/llama.cpp/llama-b11001/…, models/llm/*.gguf
tar -xzf models-htr.tar.gz -C models     # → models/htr/raxtemur__trocr-base-ru/…
```

`./models` — значение по умолчанию `LLM_DIR`, `./models/htr` — `HTR_MODELS` в `docker-compose*.yml`; другой каталог —
через эти переменные.

## 4. Запустить без сети

В каталоге с `docker-compose.yml`:

```bash
mkdir -p data/dataset data/work
# вход (необязательно): хеши паролей и секрет сессии в .env
read -rs -p "Пароль inspector: " PW; echo; printf 'INSPECTOR_PASSWORD_HASH=%s\n' "$(printf '%s' "$PW" | sha256sum | cut -d' ' -f1)" >> .env
read -rs -p "Пароль expert: " PW; echo; printf 'EXPERT_PASSWORD_HASH=%s\n' "$(printf '%s' "$PW" | sha256sum | cut -d' ' -f1)" >> .env
printf 'SESSION_SECRET=%s\n' "$(head -c 32 /dev/urandom | base64)" >> .env; unset PW

docker compose up -d --no-build                                              # сервис на CPU
curl -s http://localhost:3100/api/v1/health                                  # {"status":"ok",…}; docker compose ps → healthy
```

Экран — `http://localhost:3100/`. Документы объекта — в `./data/dataset/<папка объекта>`, на экране «Проверить папку»
с путём `/data/dataset/<папка объекта>`, или загрузка файлов с компьютера.

С языковой моделью (вместо сервиса `inspector`, нужна свободная память ≥ 6,5 ГБ):

```bash
docker compose down
docker compose --profile llm up -d --no-build inspector-llm                  # LLM_DIR=./models по умолчанию
curl -s http://localhost:3100/api/v1/health                                  # "models":{"llm":"available",…}
```

Профиль `htr` (рукопись) требует отдельного образа `ai-inspector:htr` (окружение torch/transformers): в комплект он
не входит, собирается с сетью — `docker compose -f docker-compose.yml -f docker-compose.htr.yml --profile htr build
inspector-htr`. Веса для него — `models-htr.tar.gz`.

## Скачать комплект

`https://ai.sokol-c3.ru/artifacts/<тег>/` — по логину и паролю (basic auth), которые передаются отдельно. Настройка
nginx — `ops/nginx-artifacts.conf`.

## Как собран

```bash
bash scripts/release_artifacts.sh <тег> [<git-ревизия>]
```

Образ собирается из содержимого ревизии (`git archive`), проверяется в контейнере `--network none` (healthy,
`/health` → 200) и только потом сохраняется. Проверочный прогон 27.09.2026 (T-162) — на текущем коде, тег
`t162-check`; итоговый комплект соберёт T-152 на итоговом теге.
