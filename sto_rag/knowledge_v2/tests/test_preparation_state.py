import unittest
from knowledge_v2.preparation_state import PreparationState
from knowledge_v2.store import NotReady

class PreparationStateTests(unittest.TestCase):
    def setUp(self):
        self.now=0;self.guard=PreparationState(clock=lambda:self.now)
    def status(self,state='paused',maintenance=False):
        return {'phases':[{'stage':'material','state':state}], 'resources':{'maintenance':maintenance}}
    def test_maintenance_waits_without_completing_command(self):
        for self.now in (0,100,1000):self.assertFalse(self.guard.paused(self.status(maintenance=True)))
    def test_handover_pause_is_not_user_pause(self):
        self.guard.paused(self.status(maintenance=True));self.now=100
        self.assertFalse(self.guard.paused(self.status()))
        self.now=102;self.assertFalse(self.guard.paused(self.status('running')))
    def test_persistent_pause_remains_pause(self):
        self.assertFalse(self.guard.paused(self.status()));self.now=15
        self.assertTrue(self.guard.paused(self.status()))
    def test_maintenance_resets_previous_pause_clock(self):
        self.guard.paused(self.status());self.now=20
        self.assertFalse(self.guard.paused(self.status(maintenance=True)));self.now=21
        self.assertFalse(self.guard.paused(self.status()))
    def test_failed_dependency_is_not_hidden(self):
        with self.assertRaises(NotReady):self.guard.paused(self.status('failed'))
