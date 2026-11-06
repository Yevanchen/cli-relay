"""Mock DNS/TLS/HTTP, clocks and worker lifecycle. No network or model calls."""
import json
import socket
import ssl
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import helpers
from cli_relay import notify, transport

ADAPTER = {'type':'http', 'url_env':'URL', 'token_env':'TOKEN', 'timeout_s':1}
ENV = {'URL':'https://synthetic.invalid/private', 'TOKEN':'SYNTHETIC_SECRET'}


class HTTPPhases(unittest.TestCase):
    def exchange(self, conn, dns=None):
        phases = []
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1',443))]
        with patch.object(notify.http.client, 'HTTPSConnection', return_value=conn), \
             patch.object(notify.socket, 'getaddrinfo', side_effect=dns, return_value=addresses):
            result = notify._http_exchange(ADAPTER, {}, ENV, phases.append)
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(result))
        self.assertNotIn('synthetic.invalid', json.dumps(result))
        return result, phases

    def test_dns_tls_and_response_loss_are_distinct(self):
        conn = Mock()
        result, phases = self.exchange(conn, socket.gaierror('SYNTHETIC_SECRET'))
        self.assertEqual((result['outcome'], result['why'], phases[-1]), ('retry','connect_failed','dns'))
        conn.request.assert_not_called()
        conn = Mock()
        conn.connect.side_effect = ssl.SSLEOFError('SYNTHETIC_SECRET')
        result, phases = self.exchange(conn)
        self.assertEqual((result['outcome'], result['why'], phases[-1]), ('retry','connect_failed','tls'))
        conn = Mock()
        conn.getresponse.side_effect = ssl.SSLEOFError('SYNTHETIC_SECRET')
        result, phases = self.exchange(conn)
        self.assertEqual((result['outcome'], result['why'], phases[-1]), ('ambiguous','response_lost','response_headers'))
        conn.close.assert_called_once()

    def test_http_acceptance_has_no_visible_or_ack_claim(self):
        conn = Mock()
        response = conn.getresponse.return_value
        response.status = 202
        response.getheader.return_value = None
        response.read.return_value = b'{}'
        result, phases = self.exchange(conn)
        self.assertEqual(result, {'outcome':'accepted', 'why':'http_2xx', 'status':202})
        self.assertEqual(phases[-1], 'complete')
        self.assertNotIn('message_id', result)


class TotalDeadline(unittest.TestCase):
    def supervise(self, *, may_send=False, wall_factor=1, mono_factor=1, queued_phase='dns', lost=False, unkillable=False):
        clock = {'wall':100., 'mono':100.}
        flag = SimpleNamespace(value=int(may_send))
        queue = [('phase', queued_phase)]
        process = Mock(pid=123)
        process.is_alive.return_value = not lost
        def terminate():
            process.is_alive.return_value = unkillable
        process.terminate.side_effect = terminate
        reader, writer = Mock(), Mock()
        def poll(timeout):
            if queue:
                return True
            clock['wall'] += timeout * wall_factor
            clock['mono'] += timeout * mono_factor
            return False
        reader.poll.side_effect = poll
        reader.recv.side_effect = lambda: queue.pop(0)
        ctx = Mock()
        ctx.Pipe.return_value = (reader,writer)
        ctx.Process.return_value = process
        ctx.Value.return_value = flag
        with patch.object(transport.multiprocessing, 'get_context', return_value=ctx), \
             patch.object(transport.time, 'time', side_effect=lambda:clock['wall']), \
             patch.object(transport.time, 'monotonic', side_effect=lambda:clock['mono']):
            result = transport.run_http(ADAPTER, {}, ENV)
        self.assertLessEqual(clock['mono']-100, 1.051)
        reader.close.assert_called_once()
        writer.close.assert_called()
        return result, process

    def test_dns_is_bounded_and_worker_is_stopped_before_a_safe_retry(self):
        result, process = self.supervise()
        self.assertEqual((result['outcome'],result['why'],result['phase']), ('retry','connect_failed','dns'))
        self.assertTrue(result['deadline_exceeded'])
        process.terminate.assert_called_once()

    def test_sent_request_or_undrained_phase_remains_ambiguous(self):
        # The phase pipe can lag the shared pre-request flag. Never classify this as unsent.
        for phase in ('tls','request','response_headers','response_body'):
            result, process = self.supervise(may_send=True, queued_phase=phase)
            self.assertEqual((result['outcome'], result['why']), ('ambiguous','response_lost'))
            process.terminate.assert_called_once()

    def test_wall_jump_or_suspend_gap_has_a_deadline_but_is_not_proof_of_sleep(self):
        result, _ = self.supervise(wall_factor=100, mono_factor=1)
        self.assertTrue(result['timing']['clock_divergence_observed'])
        self.assertFalse(result['timing']['suspend_verified'])
        self.assertLess(result['timing']['monotonic_elapsed_s'], 0.1)
        result, _ = self.supervise(wall_factor=-100, mono_factor=1)
        self.assertTrue(result['deadline_exceeded'])
        self.assertGreaterEqual(result['timing']['monotonic_elapsed_s'], 0.99)

    def test_lost_worker_remains_ambiguous(self):
        result, _ = self.supervise(lost=True)
        self.assertEqual((result['outcome'],result['why']), ('ambiguous','transport_worker_lost'))

    def test_unverified_worker_stop_is_never_a_safe_retry(self):
        result, process = self.supervise(unkillable=True)
        self.assertEqual((result['outcome'],result['why']), ('ambiguous','transport_worker_stop_unverified'))
        self.assertFalse(result['worker_stopped'])
        process.kill.assert_called()

    def test_timing_wraps_command_and_redacts_exception_text(self):
        adapter = {'type':'command', 'timeout_s':1}
        with patch.object(notify, 'send_command', side_effect=TimeoutError('SYNTHETIC_SECRET')):
            result = notify.send(adapter, {}, {})
        self.assertEqual(result['outcome'], 'ambiguous')
        self.assertIn('configured_timeout_s', result['timing'])
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(result))

    def test_retry_budget_and_response_loss_wake_budget_are_not_increased(self):
        cfg = {'retry':{'max_attempts':3,'base_delay_s':1,'max_delay_s':10},
               'reminder':{'ack_timeout_s':1800,'max_wakes':2}}
        self.assertEqual(notify.delivery_result(cfg, {'outcome':'retry'}, 2, 0)['state'], 'failed')
        self.assertEqual(notify.delivery_result(cfg, {'outcome':'ambiguous'}, 0, 0)['state'], 'unconfirmed')
