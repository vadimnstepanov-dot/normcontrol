"""Do not turn an owned model-maintenance pause into a portal user pause."""
import time
from .store import NotReady

class PreparationState:
    def __init__(self,clock=time.monotonic,grace=15):
        self.clock=clock;self.grace=grace;self.paused_since=None
    def paused(self,status):
        phases=[p for p in status['phases'] if p['stage'] in ('material','normative_prepare')]
        if any(p['state']=='failed' for p in phases):raise NotReady('Shared preparation failed')
        if status.get('resources',{}).get('maintenance'):
            self.paused_since=None
            return False
        if not any(p['state']=='paused' for p in phases):
            self.paused_since=None
            return False
        # The maintenance marker is removed before the resumed native thread
        # updates its phase. Give that thread a bounded handover interval.
        now=self.clock()
        if self.paused_since is None:self.paused_since=now
        return now-self.paused_since>=self.grace
