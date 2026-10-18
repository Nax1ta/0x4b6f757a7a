"""Deduplicate cron, manual and continuation triggers against durable heartbeat."""
import os
import time
from datetime import datetime
from engine import StateStore, load_env

load_env('.env')
state = StateStore(os.getenv('STATE_PATH', '.local/state.enc'), encrypted=True).read()
checked = state.get('checked_at')
age = time.time() - datetime.fromisoformat(checked).timestamp() if checked else float('inf')
proceed = age >= 200
with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
    f.write('proceed=' + str(proceed).lower() + '\n')
print('Proceed' if proceed else 'Recent heartbeat; duplicate trigger skipped')
