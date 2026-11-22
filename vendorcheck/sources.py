"""Every outside call goes through SourceClient.call().

It gives each call one of four outcomes, so silence is never read as an answer:
  FOUND        the source answered with data
  NOT_FOUND    the source answered: no such record
  UNAVAILABLE  timeout / 5xx / 429 after all retries -> the check becomes "cannot verify"
  ERROR        our request was rejected (4xx) -> also "cannot verify", and logged
It also caches each response per job (a restarted run never pays twice for a
call that already completed) and writes the cost ledger.
"""
import hashlib
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .store import Store, now_iso

FOUND, NOT_FOUND, UNAVAILABLE, ERROR, SKIPPED_BUDGET = (
    "FOUND", "NOT_FOUND", "UNAVAILABLE", "ERROR", "SKIPPED_BUDGET")


@dataclass
class Result:
    status: str
    data: object = None          # parsed JSON (or text for non-JSON sources)
    call_id: int | None = None
    url: str = ""
    retrieved_at: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == FOUND


def urllib_transport(method, url, headers, body, timeout):
    """Returns (http_status, headers_dict, body_text). Raises TimeoutError / ConnectionError."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace") if e.fp else ""
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, text
    except (socket.timeout, TimeoutError) as e:
        raise TimeoutError(str(e)) from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise TimeoutError(str(e.reason)) from e
        raise ConnectionError(str(e.reason)) from e


class BudgetExceeded(Exception):
    pass


class SourceClient:
    def __init__(self, store: Store, job_id: str, cfg: dict, transport=urllib_transport, sleep=time.sleep):
        self.store, self.job_id, self.cfg = store, job_id, cfg
        self.transport, self.sleep = transport, sleep
        self.limits = cfg["limits"]

    def _over_budget(self, estimate: float) -> str:
        job = self.store.job(self.job_id)
        spent = self.store.credits_spent(self.job_id)
        if spent + estimate > self.limits["max_credits_per_run"]:
            return f"credit limit {self.limits['max_credits_per_run']} reached (spent {spent:g})"
        if job and time.time() - job["started_at"] > self.limits["max_seconds_per_run"]:
            return f"time limit {self.limits['max_seconds_per_run']}s reached"
        return ""

    def call(self, source: str, endpoint: str, method: str, url: str, payload: dict | None = None,
             headers: dict | None = None, not_found=None) -> Result:
        """not_found(parsed_json) -> bool decides when a 200 response means 'no record'."""
        req_json = json.dumps({"method": method, "url": url, "payload": payload}, sort_keys=True)
        key = hashlib.sha256(req_json.encode()).hexdigest()[:24]

        cached = self.store.cached_call(self.job_id, key)
        if cached and cached["status"] in (FOUND, NOT_FOUND):
            return self._result(cached, url)

        estimate = float(self.cfg.get("credit_estimates", {}).get(endpoint, 1))
        reason = self._over_budget(estimate)
        if reason:
            cid = self.store.record_call(
                job_id=self.job_id, source=source, endpoint=endpoint, request_key=key, request_json=req_json,
                status=SKIPPED_BUDGET, error=reason, credits=0, attempts=0, retrieved_at=now_iso())
            return Result(SKIPPED_BUDGET, None, cid, url, now_iso(), reason)

        body = json.dumps(payload).encode() if payload is not None else None
        attempts, status, http_status, text, error, credits = 0, UNAVAILABLE, None, None, "", 0.0
        t0 = time.time()
        max_attempts = 1 + int(self.limits["max_retries"])
        while attempts < max_attempts:
            attempts += 1
            try:
                http_status, rh, text = self.transport(method, url, headers or {}, body,
                                                       self.limits["http_timeout_s"])
            except TimeoutError as e:
                error = f"timeout after {self.limits['http_timeout_s']}s: {e}"
                self.sleep(self.limits["backoff_s"] * attempts)
                continue
            except ConnectionError as e:
                error = f"connection error: {e}"
                self.sleep(self.limits["backoff_s"] * attempts)
                continue

            if http_status == 429:
                wait = float(rh.get("x-ratelimit-reset") or rh.get("retry-after") or self.limits["backoff_s"] * attempts)
                error = f"rate limited (429), waited {wait:g}s"
                self.sleep(min(wait, 60))
                continue
            if http_status >= 500:
                error = f"server error {http_status}"
                self.sleep(self.limits["backoff_s"] * attempts)
                continue

            credits = float(rh.get("x-credits-used") or (estimate if 200 <= http_status < 300 else 0))
            if http_status == 404:
                status, error = NOT_FOUND, ""
            elif 400 <= http_status < 500:
                status, error = ERROR, f"HTTP {http_status}: {text[:300]}"
            else:
                status, error = FOUND, ""
                if not_found is not None:
                    try:
                        if not_found(json.loads(text)):
                            status = NOT_FOUND
                    except (ValueError, TypeError):
                        pass
            break

        cid = self.store.record_call(
            job_id=self.job_id, source=source, endpoint=endpoint, request_key=key, request_json=req_json,
            status=status, http_status=http_status, response_text=text if status in (FOUND, NOT_FOUND) else text,
            error=error, credits=credits, attempts=attempts, latency_ms=int((time.time() - t0) * 1000),
            retrieved_at=now_iso())
        return self._result(self.store.call(cid), url)

    @staticmethod
    def _result(row, url) -> Result:
        data = None
        if row["response_text"]:
            try:
                data = json.loads(row["response_text"])
            except ValueError:
                data = row["response_text"]
        return Result(row["status"], data, row["id"], url, row["retrieved_at"], row["error"] or "")
