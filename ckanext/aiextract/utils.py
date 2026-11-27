"""Small shared helpers for the aiextract CKAN extension."""

import datetime


def utcnow() -> datetime.datetime:
    """Return the current time as naive UTC datetime.

    CKAN's ``task_status.last_updated`` and the extension's job tables store naive UTC
    timestamps, so this keeps the semantics of the deprecated ``datetime.utcnow()``.
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
