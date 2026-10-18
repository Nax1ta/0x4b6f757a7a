"""Export only room aliases to client bundles; never export SMTP settings."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import load_env

load_env('.env')
config = {'repository': os.getenv('GITHUB_REPOSITORY', 'Nax1ta/0x4b6f757a7a'),
          'rooms': [{'id': k.removeprefix('BILIBILI_ROOM_NAME_'), 'name': v} for k,v in os.environ.items() if k.startswith('BILIBILI_ROOM_NAME_')]}
for path in [Path('.local/client-defaults.json'), Path('android/app/src/main/assets/client-defaults.json')]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
