import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import delivery
import engine
import poll_notify


def observation(status='live', started='2026-10-18T10:00:00+00:00', rid='1'):
    return {'id': rid, 'name': 'Test', 'status': status, 'live_started_at': started if status=='live' else None,
            'last_live_at': started, 'checked_at': '2026-10-18T10:01:00+00:00',
            'title': 'Hello', 'cover': '', 'url': 'https://live.bilibili.com/' + rid, 'error': 'network' if status=='unknown' else None}


class Sessions(unittest.TestCase):
    def test_first_observation_live_is_not_silently_ignored(self):
        room = engine.apply_observation({}, observation())
        self.assertEqual(engine.pending_recipients(room, ['a@example.com']), ['a@example.com'])

    def test_repeat_same_session_is_deduplicated(self):
        room = engine.apply_observation({}, observation())
        engine.acknowledge(room, 'a@example.com')
        room = engine.apply_observation(room, observation())
        self.assertEqual(engine.pending_recipients(room, ['a@example.com']), [])

    def test_restart_between_polls_is_detected_without_offline_sample(self):
        room = engine.apply_observation({}, observation())
        engine.acknowledge(room, 'a@example.com')
        room = engine.apply_observation(room, observation(started='2026-10-18T11:00:00+00:00'))
        self.assertEqual(engine.pending_recipients(room, ['a@example.com']), ['a@example.com'])

    def test_network_error_keeps_last_success_and_delivery_receipts(self):
        room = engine.apply_observation({}, observation())
        engine.acknowledge(room, 'a@example.com')
        unknown = engine.apply_observation(room, observation('unknown'))
        self.assertEqual(unknown['status'], 'unknown')
        self.assertEqual(unknown['session'], room['session'])
        self.assertEqual(unknown['last_success_at'], room['last_success_at'])
        restored = engine.apply_observation(unknown, observation())
        self.assertEqual(engine.pending_recipients(restored, ['a@example.com']), [])

    def test_receipts_are_per_recipient(self):
        room = engine.apply_observation({}, observation())
        engine.acknowledge(room, 'a@example.com')
        self.assertEqual(engine.pending_recipients(room, ['a@example.com', 'b@example.com']), ['b@example.com'])

    def test_enriched_session_does_not_duplicate_mail(self):
        room = engine.apply_observation({}, observation(started=None))
        engine.acknowledge(room, 'a@example.com')
        room = engine.apply_observation(room, observation())
        self.assertEqual(engine.pending_recipients(room, ['a@example.com']), [])

    def test_missing_status_is_not_offline(self):
        with patch('engine.api_json', return_value={'code': 0, 'data': {}}):
            with self.assertRaises(RuntimeError):
                engine.fetch_room('1')

    def test_china_time_conversion(self):
        self.assertEqual(engine.live_time('2026-10-18 18:00:00'), '2026-10-18T10:00:00+00:00')
        self.assertIsNone(engine.live_time('0000-00-00 00:00:00'))


class Persistence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.json'
        self.env = patch.dict(os.environ, {'BILIBILI_ROOM_NAME_1': 'Test', 'EMAIL_TO': 'a@example.com', 'EMAIL_USER': 'sender@example.com', 'EMAIL_PASSWORD': 'not-a-real-password'}, clear=True)
        self.env.start()
        self.probe = patch('delivery.probe', return_value=465)
        self.probe.start()

    def tearDown(self):
        self.probe.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_dry_run_does_not_create_or_update_state(self):
        with patch('poll_notify.load_env'), patch('poll_notify.observe', return_value=[observation()]), patch('delivery.send') as send:
            self.assertEqual(poll_notify.run(True, self.path, False), 0)
            self.assertFalse(self.path.exists())
            send.assert_not_called()

    def test_failed_delivery_retries_even_after_room_goes_offline(self):
        with patch('poll_notify.load_env'), patch('poll_notify.observe', return_value=[observation()]), patch('delivery.send', side_effect=RuntimeError('temporary')):
            self.assertEqual(poll_notify.run(False, self.path, False), 1)
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(len(saved['outbox']), 1)
        with patch('poll_notify.load_env'), patch('poll_notify.observe', return_value=[observation('offline')]), patch('delivery.send', return_value={'result': 'accepted'}) as send:
            self.assertEqual(poll_notify.run(False, self.path, False), 0)
            send.assert_called_once()
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['outbox'], {})
        self.assertTrue(saved['rooms']['1']['last_mail_at'])

    def test_room_failure_marks_job_degraded(self):
        with patch('poll_notify.load_env'), patch('poll_notify.observe', return_value=[observation('unknown')]):
            self.assertEqual(poll_notify.run(False, self.path, False), 1)

    def test_encrypted_state_is_not_plaintext_and_wrong_key_fails(self):
        store = engine.StateStore(self.path, encrypted=True)
        state = {'version': 2, 'rooms': {'private': {'title': 'secret title'}}}
        store.write(state)
        self.assertNotIn(b'secret title', self.path.read_bytes())
        self.assertEqual(store.read(), state)
        with patch.dict(os.environ, {'EMAIL_PASSWORD': 'wrong'}):
            with self.assertRaises(Exception):
                store.read()

    def test_partial_recipient_refusal_is_not_marked_sent(self):
        room = engine.apply_observation({}, observation())
        with patch('delivery.ports', return_value=[465]), patch('delivery.cover_image', return_value=None), patch('delivery.connect') as connect:
            connect.return_value.send_message.return_value = {'a@example.com': (550, 'refused')}
            with self.assertRaises(RuntimeError):
                delivery.send(room, 'a@example.com')

    def test_queue_is_merged_when_start_time_is_enriched_after_failure(self):
        with patch('poll_notify.load_env'), patch('poll_notify.observe', return_value=[observation(started=None)]), patch('delivery.send', side_effect=RuntimeError('temporary')):
            poll_notify.run(False, self.path, False)
        with patch('poll_notify.load_env'), patch('poll_notify.observe', return_value=[observation()]), patch('delivery.send', return_value={'result': 'accepted'}) as send:
            self.assertEqual(poll_notify.run(False, self.path, False), 0)
            send.assert_called_once()
        state = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(state['outbox'], {})
        self.assertEqual(engine.pending_recipients(state['rooms']['1'], ['a@example.com']), [])

    def test_old_queue_is_retried_after_permission_is_restored(self):
        with patch('poll_notify.load_env'), patch('poll_notify.observe', return_value=[observation()]), patch('delivery.send', side_effect=RuntimeError('temporary')):
            poll_notify.run(False, self.path, False)
        state = json.loads(self.path.read_text(encoding='utf-8'))
        next(iter(state['outbox'].values()))['created'] = time.time() - 172800
        engine.atomic_json(self.path, state)
        with patch('poll_notify.load_env'), patch('poll_notify.observe', return_value=[observation('offline')]), patch('delivery.send', return_value={'result': 'accepted'}) as send:
            self.assertEqual(poll_notify.run(False, self.path, False), 0)
            send.assert_called_once()

    def test_old_observed_session_is_not_merged_with_a_later_broadcast(self):
        with patch('poll_notify.load_env'), patch('poll_notify.observe', return_value=[observation(started=None)]), patch('delivery.send', side_effect=RuntimeError('temporary')):
            poll_notify.run(False, self.path, False)
        with patch('poll_notify.load_env'), patch('poll_notify.observe', return_value=[observation('offline')]), patch('delivery.send', side_effect=RuntimeError('temporary')):
            poll_notify.run(False, self.path, False)
        with patch('poll_notify.load_env'), patch('poll_notify.observe', return_value=[observation(started='2026-10-18T12:00:00+00:00')]), patch('delivery.send', return_value={'result': 'accepted'}) as send:
            poll_notify.run(False, self.path, False)
            self.assertEqual(send.call_count, 2)

    def test_recovery_restores_later_receipt_even_from_the_same_run(self):
        import io, zipfile, cloud
        encrypted = engine.StateStore(self.path, True)
        old = {'version': 2, 'revision': 2, 'run_id': '10', 'rooms': {}, 'outbox': {'pending': {}}}
        encrypted.write(old)
        newest = dict(old, revision=3, outbox={})
        payload = encrypted.cipher().encrypt(json.dumps(newest).encode())
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('state.enc', payload)
        def response(path, **kw):
            return buffer.getvalue() if kw.get('raw') else {'artifacts': [{'id': 11, 'expired': False, 'workflow_run': {'id': 10}}]}
        with patch('cloud.api', side_effect=response):
            cloud.recover(self.path)
        restored = encrypted.read()
        self.assertEqual(restored['revision'], 3)
        self.assertEqual(restored['outbox'], {})

    def test_smtp_permission_failure_marks_job_degraded_without_live_rooms(self):
        with patch('poll_notify.load_env'), patch('poll_notify.observe', return_value=[observation('offline')]), patch('delivery.probe', side_effect=RuntimeError('550 User has no permission')):
            self.assertEqual(poll_notify.run(False, self.path, False), 1)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['smtp']['status'], 'unavailable')

    def test_one_room_failure_does_not_block_another_delivery(self):
        with patch.dict(os.environ, {'BILIBILI_ROOM_NAME_2': 'Second'}), patch('poll_notify.load_env'), patch('poll_notify.observe', return_value=[observation('unknown'), observation(rid='2')]), patch('delivery.send', return_value={'result': 'accepted'}) as send:
            self.assertEqual(poll_notify.run(False, self.path, False), 1)
            send.assert_called_once()

    def test_submission_rejection_preserves_smtp_error_code(self):
        import smtplib
        room = engine.apply_observation({}, observation())
        with patch('delivery.ports', return_value=[465]), patch('delivery.cover_image', return_value=None), patch('delivery.connect') as connect:
            connect.return_value.send_message.side_effect = smtplib.SMTPDataError(550, b'User has no permission')
            with self.assertRaisesRegex(RuntimeError, '550.*User has no permission'):
                delivery.send(room, 'a@example.com')

    def test_mail_has_routing_headers_and_stable_id(self):
        room = engine.apply_observation({}, observation())
        with patch('delivery.cover_image', return_value=None):
            a = delivery.build_message(room, 'a@example.com')
            b = delivery.build_message(room, 'a@example.com')
            self.assertEqual(a['To'], 'a@example.com')
            self.assertIsNotNone(a['Date'])
            self.assertEqual(a['Message-ID'], b['Message-ID'])
            self.assertIsNone(a['Bcc'])


if __name__ == '__main__':
    unittest.main()
