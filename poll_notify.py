"""One bounded check; encrypted state and receipts survive runs."""
import argparse
import json
import os
import sys
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import delivery
from engine import StateStore, acknowledge, apply_observation, load_env, now_iso, observe, pending_recipients


def checkpoint(store, state, remote=False):
    state['checkpoint_at'] = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    state['revision'] = int(state.get('revision', 0)) + 1
    if os.getenv('GITHUB_RUN_ID'):
        state['run_id'] = os.environ['GITHUB_RUN_ID']
    store.write(state)
    if remote and os.getenv('CLOUD_CHECKPOINT') == '1':
        from cloud import push
        last_error = None
        for attempt in range(3):
            try:
                push()
                return
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise RuntimeError('Remote checkpoint unavailable') from last_error


def run(dry_run=False, state_path=None, encrypted=None):
    load_env(Path(__file__).with_name('.env'))
    encrypted = os.getenv('STATE_ENCRYPTED', '0') == '1' if encrypted is None else encrypted
    store = StateStore(state_path or os.getenv('STATE_PATH', '.local/state.json'), encrypted=encrypted)
    state = store.read()
    rooms = [{'id': k.removeprefix('BILIBILI_ROOM_NAME_'), 'name': v} for k,v in os.environ.items() if k.startswith('BILIBILI_ROOM_NAME_')]
    if not rooms:
        raise RuntimeError('未配置房间，检查 ENV_FILE')
    errors = []
    outbox = state.setdefault('outbox', {})
    try:
        delivery.probe()
        state['smtp'] = {'status': 'ok', 'checked_at': now_iso()}
        print('SMTP authentication=ok', flush=True)
    except Exception as exc:
        state['smtp'] = {'status': 'unavailable', 'checked_at': now_iso(), 'error': str(exc)}
        errors.append('smtp')
        print('SMTP authentication=unavailable', flush=True)
    results = observe(rooms)
    promotions = {}
    for index, result in enumerate(results, 1):
        rid = result['id']
        previous = state['rooms'].get(rid, {})
        updated = apply_observation(previous, result)
        if previous.get('observed_live') and str(previous.get('session', '')).startswith('observed:') and result['status'] == 'live' and str(updated.get('session', '')).startswith('start:'):
            promotions[(rid, previous['session'])] = updated['session']
        state['rooms'][rid] = updated
        if result['status'] == 'unknown':
            errors.append('room:' + str(index))
            print('R%02d observation=unavailable' % index, flush=True)
            continue
        try:
            pending = pending_recipients(updated, delivery.recipients(rid))
        except ValueError:
            errors.append('config:' + str(index))
            print('R%02d recipients=missing' % index, flush=True)
            continue
        print('R%02d observation=ok pending=%d' % (index,len(pending)), flush=True)
        for recipient in pending:
            if dry_run:
                continue
            key = hashlib.sha256((rid + updated['session'] + recipient.casefold()).encode()).hexdigest()
            outbox.setdefault(key, {'room': dict(updated), 'recipient': recipient, 'created': time.time()})
    # Merge observed-time queues when the API later supplies the actual start time.
    for key, item in list(outbox.items()):
        current = state['rooms'].get(item['room']['id'], {})
        promotion = promotions.get((item['room']['id'], item['room'].get('session')))
        if promotion and promotion == current.get('session'):
            new_key = hashlib.sha256((current['id'] + current['session'] + item['recipient'].casefold()).encode()).hexdigest()
            existing = outbox.get(new_key)
            if existing:
                existing['created'] = min(existing['created'], item['created'])
                existing['room']['message_session'] = item['room']['session']
            else:
                old_session = item['room']['session']
                item['room'].update(current)
                item['room']['message_session'] = old_session
                outbox[new_key] = item
            del outbox[key]
    # Drain persisted deliveries independently of current room state and other rooms.
    if outbox and not dry_run:
        checkpoint(store, state, remote=True)  # Never send without a durable queue.
    for key, item in list(outbox.items()):
        if dry_run:
            continue
        rid, recipient = item['room']['id'], item['recipient']
        try:
            receipt = delivery.send(item['room'], recipient)
            updated = state['rooms'].setdefault(rid, {})
            # Acknowledgement belongs to the queued session, even after a restart.
            queue_room = item['room']
            queue_room['notified'] = dict(updated.get('notified') or {})
            acknowledge(queue_room, recipient)
            updated['notified'] = queue_room['notified']
            updated['last_mail_at'] = queue_room['last_mail_at']
            updated['last_receipt'] = receipt
            updated.pop('mail_error', None)
            state['last_mail_at'] = updated['last_mail_at']
            del outbox[key]
            checkpoint(store, state, remote=True)
            print('submission=accepted', flush=True)
        except Exception as exc:
            item['error'] = str(exc)
            errors.append('mail')
            print('submission=failed (%s)' % type(exc).__name__, flush=True)
    if not dry_run:
        checkpoint(store, state)
    state['checked_at'] = now_iso()
    state['health'] = {'status': 'degraded' if errors else 'ok', 'errors': errors, 'checked_at': state['checked_at']}
    if not dry_run:
        checkpoint(store, state)
    summary = {'status': state['health']['status'], 'successful': len(results)-sum(r['status']=='unknown' for r in results), 'total': len(results), 'dry_run': dry_run}
    print(json.dumps(summary), flush=True)
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write('### Check\n\nResult: **%s** · Observations: %d/%d · Submissions: %s\n' % (summary['status'],summary['successful'],summary['total'],'preview' if dry_run else 'see step log'))
    return 1 if errors else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        sys.exit(run(args.dry_run))
    except Exception as exc:
        print('Check failed: ' + type(exc).__name__ + '. Inspect configuration or encrypted diagnostic state.', file=sys.stderr)
        sys.exit(1)
