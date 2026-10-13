# Wayback Machine Historical Backfill - CDX timestamp discovery
# Ported from the original scripts/web_archive_capture.py (credit for the
# core approach: https://github.com/citp/PrivacyPoliciesOverTime), with two
# changes: rate-limit backoff is reported to the caller instead of retried
# via recursion (so archive_backfill/pipeline.py can checkpoint attempts to
# Mongo between tries instead of blocking in-process), and settings come
# from archive_backfill/settings.py instead of module-level globals.
#
# Queries the CDX API for every snapshot of a single url and selects up to
# two per year (an "A" snapshot centered on April 1st, a "B" snapshot
# centered on October 1st) -- enough to catch most policy changes without
# fetching every single crawl the Wayback Machine has for a heavily-crawled
# domain.

import urllib.parse
from datetime import datetime

import requests

from archive_backfill.settings import CFG, logger

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Ubuntu Chromium/76.0.3809.100 Chrome/76.0.3809.100 Safari/537.36",  # noqa
    "Accept-Language": "en-US,en;q=0.5",
    "Cache-Control": "no-cache",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",  # noqa
    "Pragma": "no-cache",
}


class CdxError(Exception):
    """Raised when the CDX API can't be queried successfully, even after backoff."""


class RateLimited(Exception):
    """
    Raised on an HTTP 429 from web.archive.org (CDX or snapshot fetch).
    Deliberately NOT a CdxError/FetchError: it isn't retried in-process and
    isn't recorded as a per-row failure -- archive_backfill/pipeline.py ends
    the whole run on it instead. Every request sent while blocked appears to
    extend the block, so backing off and retrying only makes it worse.
    """


def get_cdx_url(cdx_params: list) -> str:
    base_address = "http://web.archive.org/cdx/search/cdx"
    return "%s?%s" % (base_address, urllib.parse.urlencode(cdx_params))


def load_cdx_page(cdx_params: list) -> str:
    """
    Sends a single request to the CDX API. Raises CdxError on any failure
    condition the original script treated as retry-worthy (rate limiting,
    blocked site, archive.org internal errors, empty response).
    """
    cdxurl = get_cdx_url(cdx_params)
    r = requests.get(cdxurl, headers=HTTP_HEADERS, timeout=30)
    body = r.text if r is not None else ""

    if r is not None and r.status_code == 429:
        raise RateLimited(f"HTTP 429 at {cdxurl}")
    if r is not None and r.status_code == requests.codes.ok and body:
        return body
    if "Blocked Site Error" in body:
        raise CdxError(f"Blocked Site Error at {cdxurl}")
    if (
        "org.archive.wayback.exception" in body
        or "java.net.SocketTimeoutException" in body
    ):
        raise CdxError(f"CDX error at {cdxurl}: {body[:500]}")
    raise CdxError(f"HTTP/empty response error at {cdxurl}: {body[:500]}")


def is_valid_wb_timestamp(ts: str) -> bool:
    try:
        datetime.strptime(ts, "%Y%m%d%H%M%S")
        return True
    except (ValueError, TypeError):
        return False


def date_from_ts_str(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y%m%d%H%M%S")


def select_timestamp_for_interval(
    url, year, season, ok_timestamps_datetime, non_ok_timestamps_datetime
):
    if season == "A":
        middle_point = date_from_ts_str("%s0401000000" % year)
        min_point = date_from_ts_str("%s0101000000" % year)
        max_point = date_from_ts_str("%s0630235959" % year)
    else:
        middle_point = date_from_ts_str("%s1001000000" % year)
        min_point = date_from_ts_str("%s0701000000" % year)
        max_point = date_from_ts_str("%s1231235959" % year)

    if len(ok_timestamps_datetime):
        ts = min(
            ok_timestamps_datetime,
            key=lambda x: abs((x - middle_point).total_seconds()),
        )
        if min_point < ts < max_point:
            return ts.strftime("%Y%m%d%H%M%S")

    # No snapshot with status=200 in this window -- fall back to a non-200
    # snapshot (e.g. a redirect) if one is available, since it still marks
    # that *something* was at this url at this point in time.
    if not len(non_ok_timestamps_datetime):
        return None

    ts = min(
        non_ok_timestamps_datetime,
        key=lambda x: abs((x - middle_point).total_seconds()),
    )
    if min_point < ts < max_point:
        logger.info(
            f"No snapshots with status=200, fell back to non-200 results {url} {year} {season}"
        )
        return ts.strftime("%Y%m%d%H%M%S")
    return None


def select_timestamps(
    url, ok_timestamps, non_ok_timestamps, start_year, end_year
) -> list:
    selected = []
    if not len(ok_timestamps) and not len(non_ok_timestamps):
        return selected

    ok_timestamps_datetime = [date_from_ts_str(ts) for ts in ok_timestamps]
    non_ok_timestamps_datetime = [date_from_ts_str(ts) for ts in non_ok_timestamps]
    for year in range(start_year, end_year + 1):
        for season in ["A", "B"]:
            ts = select_timestamp_for_interval(
                url, year, season, ok_timestamps_datetime, non_ok_timestamps_datetime
            )
            if ts is not None:
                selected.append(ts)
    return selected


def get_timestamps(url: str, start_year: int = 0, end_year: int = 0) -> list:
    """
    Queries and parses the CDX response for a single url, returning up to
    two selected snapshot timestamps per year in range.

    Raises CdxError if the CDX API request itself failed. Returns an empty
    list (not an error) if the request succeeded but the url has no archived
    snapshots at all.
    """
    ok_timestamps = []
    non_ok_timestamps = []
    cdx_params = [
        ("url", url),
        ("filter", "statuscode:200|301|302|warc/revisit"),
        ("filter", "!length:-"),
        ("collapse", "timestamp:6"),
    ]
    if start_year:
        cdx_params.append(("from", "%s0101" % start_year))
    if end_year:
        cdx_params.append(("to", "%s1231" % end_year))

    body = load_cdx_page(cdx_params)

    for line in body.split("\n"):
        if not line:
            continue
        items = line.rstrip().split(" ")
        timestamp = items[1]
        if not is_valid_wb_timestamp(timestamp):
            logger.warning(f"Invalid CDX timestamp for {url}: {timestamp}")
            continue
        status_code = items[-3]
        if status_code == "200":
            ok_timestamps.append(timestamp)
        else:
            non_ok_timestamps.append(timestamp)

    return select_timestamps(
        url, ok_timestamps, non_ok_timestamps, start_year, end_year
    )


def discover(url: str) -> list:
    """
    Rate-limit-respecting wrapper around get_timestamps for a single url.
    On a CDX failure, sleeps CFG.CDX_BACKOFF_SECONDS and retries exactly
    once more before propagating the error -- the Wayback Machine's rate
    limiting is usually a short-lived slowdown, so one longer sleep clears
    most transient failures without this job's own retry-batch bookkeeping
    (see archive_backfill/db.py) having to spin through several full
    executions for what was really a single hiccup.
    """
    import time

    time.sleep(CFG.CDX_SLEEP_SECONDS)
    try:
        return get_timestamps(url, CFG.START_YEAR, CFG.END_YEAR)
    except CdxError as e:
        logger.warning(
            f"CDX request failed for {url}, backing off {CFG.CDX_BACKOFF_SECONDS}s: {e}"
        )
        time.sleep(CFG.CDX_BACKOFF_SECONDS)
        return get_timestamps(url, CFG.START_YEAR, CFG.END_YEAR)
