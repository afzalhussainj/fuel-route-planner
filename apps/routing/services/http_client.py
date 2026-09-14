from __future__ import annotations

import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


def build_httpx_client(
    *,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Reusable httpx client with explicit connect/read timeouts."""
    connect = float(settings.HTTP_CONNECT_TIMEOUT_SECONDS)
    read = float(settings.HTTP_READ_TIMEOUT_SECONDS)
    timeout = httpx.Timeout(connect=connect, read=read, write=read, pool=connect)
    return httpx.Client(timeout=timeout, transport=transport)


def is_retryable_status(status_code: int) -> bool:
    return status_code in {429, 500, 502, 503, 504}


def request_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_retries: int | None = None,
    **kwargs,
) -> httpx.Response:
    """
    Retry only genuinely transient failures.

    Does not treat ordinary 4xx (except 429) as retryable.
    """
    retries = int(settings.HTTP_MAX_RETRIES) if max_retries is None else max_retries
    attempt = 0
    while True:
        try:
            response = client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            if attempt >= retries:
                raise
            attempt += 1
            logger.warning(
                "Retrying upstream %s after transport timeout/error (%s/%s)",
                method,
                attempt,
                retries,
            )
            continue

        if is_retryable_status(response.status_code) and attempt < retries:
            attempt += 1
            logger.warning(
                "Retrying upstream %s after HTTP %s (%s/%s)",
                method,
                response.status_code,
                attempt,
                retries,
            )
            continue
        return response
