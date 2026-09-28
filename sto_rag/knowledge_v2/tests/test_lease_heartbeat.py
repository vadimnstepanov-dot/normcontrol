import threading
import unittest
import urllib.error

from knowledge_v2.lease_heartbeat import renew_analysis_lease


class ClockAndStop:
    def __init__(self, steps):
        self.now = 0
        self.steps = steps
        self.delays = []

    def wait(self, seconds):
        if self.steps == 0:
            return True
        self.steps -= 1
        self.delays.append(seconds)
        self.now += seconds
        return False


class LeaseHeartbeatTests(unittest.TestCase):
    def run_renewal(self, responses, *, steps=3, lease_seconds=600):
        stop = ClockAndStop(steps)
        lost = threading.Event()
        calls = []
        def transport(path, payload):
            calls.append((path, payload))
            value = responses[min(len(calls)-1, len(responses)-1)]
            if isinstance(value, Exception):
                raise value
            return value
        renew_analysis_lease(transport, {'command_id':'same-command', 'lease':'same-lease'},
                             stop, lost, clock=lambda:stop.now, lease_seconds=lease_seconds)
        return stop, lost, calls

    def test_temporary_outage_recovers_without_losing_lease(self):
        stop, lost, calls = self.run_renewal([urllib.error.URLError('offline'), {}, {}])
        self.assertFalse(lost.is_set())
        self.assertEqual(stop.delays, [90, 5, 90])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(x[1]['command_id']=='same-command' for x in calls))

    def test_restart_gateway_error_is_retryable(self):
        error = urllib.error.HTTPError('https://portal',502,'gateway',{},None)
        _, lost, calls = self.run_renewal([error, {}], steps=2)
        self.assertFalse(lost.is_set())
        self.assertEqual(len(calls), 2)

    def test_revocation_and_stale_lease_stop_immediately(self):
        for code in (401, 403, 409):
            with self.subTest(code=code):
                error = urllib.error.HTTPError('https://portal',code,'revoked',{},None)
                _, lost, calls = self.run_renewal([error, {}])
                self.assertTrue(lost.is_set())
                self.assertEqual(len(calls), 1)

    def test_outage_cannot_continue_beyond_lease_budget(self):
        _, lost, calls = self.run_renewal([urllib.error.URLError('offline')],steps=30,lease_seconds=140)
        self.assertTrue(lost.is_set())
        self.assertEqual(len(calls), 4)

    def test_normal_stop_does_not_report_lease_loss(self):
        _, lost, calls = self.run_renewal([{}],steps=0)
        self.assertFalse(lost.is_set())
        self.assertEqual(calls, [])
