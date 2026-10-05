"""Bounded lease renewal through temporary network outages, never through revocation."""
import time
import logging
import urllib.error


def renew_analysis_lease(transport, claim, stopped, lost, *, interval=90,
                        lease_seconds=600, safety_seconds=30, clock=time.monotonic, observe=None):
    # The claim was just received. Use monotonic elapsed time rather than comparing
    # clocks of the portal and worker hosts. Every successful renewal resets it.
    def notice(state, **details):
        if observe:
            try: observe(dict(state=state, at=time.time(), **details))
            except Exception: logging.warning("Lease diagnostic write failed")
    last_success = clock()
    delay = interval
    while not stopped.wait(delay):
        if clock() - last_success >= lease_seconds - safety_seconds:
            lost.set()
            notice('lost', reason='renewal_budget_exhausted')
            return
        requested = clock()
        try:
            transport('/worker/renew/', {'command_id': claim['command_id'],
                                       'lease': claim['lease']})
        except urllib.error.HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504):
                lost.set()
                notice('lost', reason='http_rejected', status=error.code)
                return
            notice('retry', reason='http_temporary', status=error.code)
            delay = 5
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            notice('retry', reason='transport_unavailable')
            delay = 5
        except Exception:
            lost.set()
            notice('lost', reason='unexpected_renewal_error')
            return
        else:
            # The server may extend the lease before a slow response arrives.
            # Conservatively account from request start, not response receipt.
            last_success = requested
            if clock() - last_success >= lease_seconds - safety_seconds:
                lost.set()
                notice('lost', reason='renewal_response_too_late')
                return
            notice('renewed')
            delay = interval


class ReviewLeaseLost(Exception):
    """Stop a review whose command can no longer be renewed."""


def check_review_lease(cancel):
    if cancel is not None and cancel():
        raise ReviewLeaseLost("Review worker lease lost; saved results retained")
