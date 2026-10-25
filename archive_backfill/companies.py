# Wayback Machine Historical Backfill - reading a company's doc_urls
#
# source/utils.py's format_data() (the live pipeline) expects every doc_urls
# entry to already be {"name": ..., "urls": [...]} -- see commit 7445666
# ("Support multi-URL doc types"), which migrated the CODE to that shape so
# a doc type split across multiple pages can list more than one seed url.
# As of this job's rollout, however, every company actually in the live
# database still has the OLDER shape: doc_urls[name] is a bare url string,
# not a dict. (Confirmed against the real companies collection while
# building mark_precaptured.py -- worth flagging to whoever owns the
# companies-schema migration, since it means source/utils.py's format_data()
# would currently raise on real data too.)
#
# This job tolerates both shapes so it keeps working regardless of which one
# is actually live at any given time, and doesn't need updating again once
# that migration does happen.

DocUrlEntry = str | dict | None


def slugify_doc_type(name: str) -> str:
    """Matches source/utils.py format_data's own doc_type -> "type" field convention."""
    return name.replace(" ", "_").lower()


def urls_from_entry(entry: DocUrlEntry) -> list:
    if isinstance(entry, str):
        return [entry] if entry else []
    if isinstance(entry, dict):
        return entry.get("urls") or []
    return []


def iter_doc_type_urls(company: dict):
    """Yields (doc_type_slug, [urls]) for every doc_urls entry that has at least one url."""
    for name, entry in (company.get("doc_urls") or {}).items():
        urls = urls_from_entry(entry)
        if urls:
            yield slugify_doc_type(name), urls


def find_urls_for_doc_type(company: dict, doc_type: str) -> list:
    """Returns the url list for the doc_urls entry whose slug matches doc_type, or []."""
    for slug, urls in iter_doc_type_urls(company):
        if slug == doc_type:
            return urls
    return []
