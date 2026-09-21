"""Prevent automatic Windows sleep while a local normcontrol job is alive."""
import argparse
import ctypes
import json
import time
import urllib.request


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ACTIVE_STATES = {"preparing", "running"}


def load_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def active_job(base_url, now=None, max_heartbeat_age=30):
    """Return the first live job, ignoring stale state left after a crash."""
    now = time.time() if now is None else now
    base_url = base_url.rstrip("/")
    for item in load_json(base_url + "/api/jobs"):
        if item.get("state") not in ACTIVE_STATES:
            continue
        status = load_json(base_url + "/api/jobs/" + item["id"])
        heartbeat = status.get("heartbeat")
        if (status.get("state") in ACTIVE_STATES and not status.get("stalled")
                and isinstance(heartbeat, (int, float))
                and 0 <= now - heartbeat <= max_heartbeat_age):
            return {"id": item["id"], "state": status["state"],
                    "heartbeat_age": round(now - heartbeat, 1)}
    return None


class WindowsSleepRequest:
    def __init__(self):
        self.held = False
        self.kernel32 = ctypes.windll.kernel32 if hasattr(ctypes, "windll") else None

    def set(self, active):
        if active == self.held:
            return True
        if self.kernel32 is None:
            return False
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if active else ES_CONTINUOUS
        if not self.kernel32.SetThreadExecutionState(flags):
            raise ctypes.WinError()
        self.held = active
        return True

    def close(self):
        if self.held:
            self.set(False)


def run(base_url, interval=10):
    request = WindowsSleepRequest()
    last_id = None
    try:
        while True:
            try:
                job = active_job(base_url)
            except Exception:
                job = None
            request.set(bool(job))
            job_id = job["id"] if job else None
            if job_id != last_id:
                print(("S3 blocked for normcontrol job " + job_id) if job_id
                      else "S3 block released; no live normcontrol job", flush=True)
                last_id = job_id
            time.sleep(interval)
    finally:
        request.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:8096")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    if args.probe:
        job = active_job(args.base_url)
        request = WindowsSleepRequest()
        supported = request.set(bool(job))
        result = {"active_job": job, "sleep_blocked": bool(job) and supported}
        request.close()
        print(json.dumps(result, ensure_ascii=False))
        return
    run(args.base_url)


if __name__ == "__main__":
    main()
