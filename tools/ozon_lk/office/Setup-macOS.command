#!/bin/bash
set -eu
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python collect.py login oz_acc1
.venv/bin/python collect.py login oz_acc2
.venv/bin/python collect.py poll all
.venv/bin/python collect.py poll all
.venv/bin/python - <<'PY'
import os, plistlib
from pathlib import Path
folder = Path.cwd()
dest = Path.home() / "Library/LaunchAgents/ru.rev.ozon-reviews.plist"
dest.parent.mkdir(parents=True,exist_ok=True)
config = dict(Label="ru.rev.ozon-reviews", ProgramArguments=[str(folder/".venv/bin/python"),str(folder/"collect.py"),"poll","all"], StartInterval=900)
if os.environ.get("REV_OZON_SSH"):
    config["EnvironmentVariables"] = {"REV_OZON_SSH": os.environ["REV_OZON_SSH"]}
dest.write_bytes(plistlib.dumps(config))
PY
domain="gui/$(id -u)"
launchctl bootout "$domain/ru.rev.ozon-reviews" 2>/dev/null || true
launchctl bootstrap "$domain" "$HOME/Library/LaunchAgents/ru.rev.ozon-reviews.plist"
echo "Ready: ru.rev.ozon-reviews (15 min)"
