"""Template helpers for the aiextract CKAN extension."""

import re

import requests
from ckan.plugins import toolkit

from ckanext.aiextract.logic.action import DEFAULT_FORMATS


def common_member(a, b):
    """Return True if any element of ``a`` is contained in ``b``."""
    return any(i in b for i in a)


def aiextract_show_tools(resource):
    """Return True if the resource format matches one of the supported formats."""
    format_parts = re.split("/|;", resource["format"].lower().replace(" ", ""))
    return bool(common_member(format_parts, DEFAULT_FORMATS))


def aiextract_service_available():
    """Return True if the configured extract service URL responds to a HEAD request."""
    extract_url = toolkit.config.get("ckanext.aiextract.extract_url", "")
    if not extract_url:
        return False  # If EXTRACT_URL is not set, return False
    try:
        # Perform a HEAD request (lightweight check) to see if the service responds
        response = requests.head(extract_url, timeout=5)
        # True if reachable with a valid status code, False otherwise
        return bool((200 <= response.status_code < 400) or response.status_code == 405)
    except requests.RequestException:
        # If there's any issue (timeout, connection error, etc.)
        return False


def get_helpers():
    """Return the template helpers provided by this extension."""
    return {
        "aiextract_show_tools": aiextract_show_tools,
    }
