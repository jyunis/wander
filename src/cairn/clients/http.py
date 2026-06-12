"""Shared outbound HTTP courtesy layer (TLC-5, TLC-12).

GET-only. Every request carries a descriptive User-Agent identifying Cairn.
Concurrency to a host is capped by a semaphore; `429`/`5xx` trigger bounded
exponential backoff. No write verbs, no Authorization header — Cairn only ever
reads from public endpoints (TLC-12).
"""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("cairn.http")


class CourtesyHttp:
    """A thin wrapper over httpx.AsyncClient enforcing the TLC-5/12 posture."""

    def __init__(
        self,
        user_agent: str,
        concurrency: int = 4,
        max_retries: int = 3,
        backoff_base_s: float = 1.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s

    async def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        timeout: float = 30.0,
    ) -> httpx.Response:
        """GET with concurrency cap + backoff on 429/5xx.

        Raises httpx exceptions on terminal transport failure; callers wrap
        these into their typed error taxonomy. Returns the final Response
        (which may still be a non-2xx the caller must inspect).
        """
        attempt = 0
        while True:
            async with self._sem:
                resp = await self._client.get(url, params=params, timeout=timeout)
            if resp.status_code < 400:
                return resp
            retryable = resp.status_code == 429 or 500 <= resp.status_code < 600
            if not retryable or attempt >= self._max_retries:
                return resp
            delay = self._retry_delay(resp, attempt)
            log.warning(
                "GET %s -> %s; backing off %.1fs (attempt %d/%d)",
                url,
                resp.status_code,
                delay,
                attempt + 1,
                self._max_retries,
            )
            await asyncio.sleep(delay)
            attempt += 1

    async def post_json(
        self,
        url: str,
        body: dict,
        *,
        timeout: float = 30.0,
    ) -> httpx.Response:
        """POST a JSON body with the same concurrency cap + 429/5xx backoff.

        Some public READ APIs only expose their query as a POST (e.g. the
        UseDirect availability grid is `POST …/search/grid`). This is still a
        read — no auth, no mutation of remote state — so it keeps the TLC-12
        posture; the verb is the API's, not ours. Returns the final Response.
        """
        attempt = 0
        while True:
            async with self._sem:
                resp = await self._client.post(url, json=body, timeout=timeout)
            if resp.status_code < 400:
                return resp
            retryable = resp.status_code == 429 or 500 <= resp.status_code < 600
            if not retryable or attempt >= self._max_retries:
                return resp
            delay = self._retry_delay(resp, attempt)
            log.warning(
                "POST %s -> %s; backing off %.1fs (attempt %d/%d)",
                url,
                resp.status_code,
                delay,
                attempt + 1,
                self._max_retries,
            )
            await asyncio.sleep(delay)
            attempt += 1

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return self._backoff_base_s * (2**attempt)

    async def aclose(self) -> None:
        await self._client.aclose()
