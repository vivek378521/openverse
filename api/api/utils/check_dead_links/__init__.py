import asyncio
import time

from django.conf import settings

import aiohttp
import django_redis
import structlog
from asgiref.sync import async_to_sync
from decouple import config
from elasticsearch_dsl.response import Hit
from redis.exceptions import ConnectionError

from api.utils.aiohttp import get_aiohttp_session
from api.utils.check_dead_links.provider_status_mappings import provider_status_mappings
from api.utils.dead_link_mask import get_query_mask, save_query_mask


logger = structlog.get_logger(__name__)
head_logger = structlog.get_logger(f"{__name__}._head")

CACHE_PREFIX = "valid:"
HEADERS = {
    "User-Agent": settings.OUTBOUND_USER_AGENT_TEMPLATE.format(purpose="LinkValidation")
}


def _get_cached_statuses(redis, urls):
    try:
        cached_statuses = redis.mget([CACHE_PREFIX + url for url in urls])
        return [
            int(b.decode("utf-8")) if b is not None else None for b in cached_statuses
        ]
    except ConnectionError:
        logger.warning("Redis connect failed, validating all URLs without cache.")
        return [None] * len(urls)


def _get_expiry(status, default):
    return config(f"LINK_VALIDATION_CACHE_EXPIRY__{status}", default=default, cast=int)


_timeout = aiohttp.ClientTimeout(total=settings.LINK_VALIDATION_TIMEOUT_SECONDS)
_TIMEOUT_STATUS = -2
_ERROR_STATUS = -1


async def _head(
    url: str, session: aiohttp.ClientSession, provider: str
) -> tuple[str, int]:
    start_time = time.perf_counter()

    try:
        response = await session.head(
            url, allow_redirects=False, headers=HEADERS, timeout=_timeout
        )
        status = response.status
    except (aiohttp.ClientError, asyncio.TimeoutError) as exception:
        if not isinstance(exception, asyncio.TimeoutError):
            head_logger.error("dead_link_validation_error", e=exception)
            status = _ERROR_STATUS
        else:
            status = _TIMEOUT_STATUS

    end_time = time.perf_counter()
    logger.info(
        "dead_link_validation_timing",
        url=url,
        status=status,
        time=end_time - start_time,
        provider=provider,
    )

    return url, status


# https://stackoverflow.com/q/55259755
@async_to_sync
async def _make_head_requests(
    urls: dict[str, int], results: list[Hit]
) -> list[tuple[str, int]]:
    """
    Concurrently HEAD request the urls.

    ``urls`` must map to the index of the corresponding result in ``results``.

    :param urls: A dictionary with keys of the URLs to request, mapped to the index of that url in ``results``
    :param results: The ordered list of results, including ones not being validated.
    """
    session = await get_aiohttp_session()
    tasks = [
        asyncio.ensure_future(_head(url, session, results[idx].provider))
        for url, idx in urls.items()
    ]
    responses = asyncio.gather(*tasks)
    await responses
    return responses.result()


def check_dead_links(query_hash: str, start_slice: int, results: list[Hit]) -> None:
    """
    Make sure images exist before we display them.

    Treat redirects as broken links since most of the time the redirect leads to a
    generic "not found" placeholder.

    Results are cached in redis and shared amongst all API servers in the
    cluster.
    """
    if not results:
        logger.info("link_validation_empty_results")
        return

    urls = [result.url for result in results]

    logger.debug("starting validation")
    start_time = time.time()

    # Pull matching images from the cache.
    redis = django_redis.get_redis_connection("default")
    cached_statuses = _get_cached_statuses(redis, urls)
    logger.debug(f"len(cached_statuses)={len(cached_statuses)}")

    # Anything that isn't in the cache needs to be validated via HEAD request.
    to_verify = {}
    for idx, url in enumerate(urls):
        if cached_statuses[idx] is None:
            to_verify[url] = idx
    logger.debug(f"len(to_verify)={len(to_verify)}")

    verified = _make_head_requests(to_verify, results)

    # Cache newly verified image statuses.
    to_cache = {CACHE_PREFIX + url: status for url, status in verified}

    pipe = redis.pipeline()
    if len(to_cache) > 0:
        pipe.mset(to_cache)

    for key, status in to_cache.items():
        if status == 200:
            logger.debug(f"healthy link key={key}")
        elif status == _TIMEOUT_STATUS or status == _ERROR_STATUS:
            logger.debug(f"no response from provider key={key}")
        else:
            logger.debug(f"broken link key={key}")

        expiry = settings.LINK_VALIDATION_CACHE_EXPIRY_CONFIGURATION[status]
        logger.debug(f"caching status={status} expiry={expiry}")
        pipe.expire(key, expiry)

    try:
        pipe.execute()
    except ConnectionError:
        logger.warning("Redis connect failed, cannot cache link liveness.")

    # Merge newly verified results with cached statuses
    for idx, url in enumerate(to_verify):
        cache_idx = to_verify[url]
        cached_statuses[cache_idx] = verified[idx][1]

    # Create a new dead link mask
    new_mask = [1] * len(results)

    # Delete broken images from the search results response.
    for idx, _ in enumerate(cached_statuses):
        del_idx = len(cached_statuses) - idx - 1
        status = cached_statuses[del_idx]

        provider = results[del_idx]["provider"]
        status_mapping = provider_status_mappings[provider]

        if status in status_mapping.unknown:
            logger.warning(
                "Image validation failed due to rate limiting or blocking. "
                f"url={urls[idx]} "
                f"status={status} "
                f"provider={provider} "
            )
        elif status not in status_mapping.live:
            logger.info(
                "Deleting broken image from results "
                f"id={results[del_idx]['identifier']} "
                f"status={status} "
                f"provider={provider} "
            )
            # remove the result, mutating in place
            del results[del_idx]
            # update the result's position in the mask to indicate it is dead
            new_mask[del_idx] = 0

    # Merge and cache the new mask
    mask = get_query_mask(query_hash)
    if mask:
        # skip the leading part of the mask that represents results that come before
        # the results we've verified this time around. Overwrite everything after
        # with our new results validation mask.
        new_mask = mask[:start_slice] + new_mask
    save_query_mask(query_hash, new_mask)

    end_time = time.time()
    logger.debug(
        "end validation "
        f"end_time={end_time} "
        f"start_time={start_time} "
        f"delta={end_time - start_time} "
    )
