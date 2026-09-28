"""Bounded lease renewal through temporary network outages, never through revocation."""
import time
import urllib.error


def renew_analysis_lease(transport, claim, stopped, lost, *, interval=90,
                        lease_seconds=600, safety_seconds=30, clock=time.monotonic):
    # The claim was just received. Use monotonic elapsed time rather than comparing
    # clocks of the portal and worker hosts. Every successful renewal resets it.
    last_success = clock()
    delay = interval
    while not stopped.wait(delay):
        if clock() - last_success >= lease_seconds - safety_seconds:
            lost.set()
            return
        try:
            transport('/worker/renew/', {'command_id': claim['command_id'],
                                       'lease': claim['lease']})
        except urllib.error.HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504):
                lost.set()
                return
            delay = 5
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            delay = 5
        except Exception:
            lost.set()
            return
        else:
            last_success = clock()
            delay = interval
