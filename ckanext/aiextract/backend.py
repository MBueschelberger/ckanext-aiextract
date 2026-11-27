"""Backend helpers for downloading, extracting and uploading CKAN resources."""

import logging
import os
from contextlib import contextmanager
from io import BytesIO
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests
from ckan.plugins import toolkit
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from werkzeug.datastructures import FileStorage

# Configuration constants
CKAN_URL = os.environ.get("CKAN_SITE_URL", "http://localhost:5000")
SSL_VERIFY = toolkit.asbool(toolkit.config.get("ckanext.aiextract.ssl_verify", True))
CHUNK_SIZE = 16 * 1024  # 16kb

# Timeout configuration
DEFAULT_TIMEOUT = 300  # 5 minutes
DOWNLOAD_TIMEOUT = 600  # 10 minutes for downloads
UPLOAD_TIMEOUT = 600  # 10 minutes for uploads
EXTRACTION_TIMEOUT = 1800  # 30 minutes for extractions

log = logging.getLogger(__name__)

if not SSL_VERIFY:
    requests.packages.urllib3.disable_warnings()


class BackendError(Exception):
    """Base exception for backend errors"""


class DownloadError(BackendError):
    """Exception for download errors"""


class UploadError(BackendError):
    """Exception for upload errors"""


class ExtractionError(BackendError):
    """Exception for extraction errors"""


def create_session_with_timeout() -> requests.Session:
    """
    Creates a requests Session with retry strategy and timeouts.

    Returns:
        requests.Session: Configured session
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


@contextmanager
def managed_session():
    """Context Manager for session management"""
    session = create_session_with_timeout()
    try:
        yield session
    finally:
        session.close()


def prepare_headers(api_key: str) -> dict[str, str]:
    """
    Prepares HTTP headers with API key.

    Args:
        api_key: API key in format 'header:key' or just 'key'

    Returns:
        Dict with headers
    """
    headers = {}
    if api_key:
        if ":" in api_key:
            header, key = api_key.split(":", 1)
        else:
            header, key = "Authorization", api_key
        headers[header] = key
    return headers


def get_first_in_nested(data: dict | list | str, key: str) -> Any:
    """
    Recursively searches for a key in nested data structures.

    Args:
        data: Nested dictionary/list structure or string
        key: Key to search for

    Returns:
        Value of the key or None if not found
    """
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for value in data.values():
            result = get_first_in_nested(value, key)
            if result is not None:
                return result
    elif isinstance(data, list):
        for item in data:
            result = get_first_in_nested(item, key)
            if result is not None:
                return result

    return None


def get_title(data: dict[str, Any]) -> str | None:
    """
    Extracts title from PDF2MD metadata.

    Args:
        data: Metadata dictionary

    Returns:
        Title string or None
    """
    try:
        cross_ref_data = data.get("raw_crossref_data", None)
        toc = data.get("table_of_contents", None)

        if cross_ref_data and "title" in cross_ref_data:
            title_data = cross_ref_data["title"]
            if isinstance(title_data, list):
                return " ".join(title_data)
            return str(title_data)

        if toc and isinstance(toc, list):
            for item in toc:
                if (
                    item.get("heading_level") is None
                    and item.get("title")
                    and len(item["title"].split()) >= 5
                ):
                    return item["title"]

    except (KeyError, TypeError, AttributeError) as e:
        log.warning("Error extracting title: %s", e)

    return None


def get_authors(data: dict[str, Any]) -> str | None:
    """
    Extracts authors from PDF2MD metadata.

    Args:
        data: Metadata dictionary

    Returns:
        Authors string or None
    """
    try:
        cross_ref_data = data.get("raw_crossref_data", None)

        if cross_ref_data and "author" in cross_ref_data:
            author_obj = cross_ref_data["author"]
            if isinstance(author_obj, list):
                authors = []
                for author in author_obj:
                    if isinstance(author, dict) and "given" in author and "family" in author:
                        authors.append(f"{author['given']} {author['family']}")
                return ", ".join(authors) if authors else None

        # Fallback to nested search
        return get_first_in_nested(data, "author")

    except (KeyError, TypeError, AttributeError) as e:
        log.warning("Error extracting authors: %s", e)

    return None


def find_first_matching_dict(
    dicts: list[dict[str, Any]], match_dict: dict[str, str]
) -> dict[str, Any] | None:
    """
    Finds the first dictionary matching the search criteria.

    Args:
        dicts: List of dictionaries to search
        match_dict: Search criteria

    Returns:
        First matching dictionary or None
    """
    for d in dicts:
        if all(d.get(k, "").strip('"') == v.strip('"') for k, v in match_dict.items()):
            return d
    return None


def find_matching_dicts(
    dicts: list[dict[str, Any]], match_dict: dict[str, str]
) -> list[dict[str, Any]]:
    """
    Finds all dictionaries matching the search criteria.

    Args:
        dicts: List of dictionaries to search
        match_dict: Search criteria

    Returns:
        List of matching dictionaries
    """
    result = []
    for d in dicts:
        if all(d.get(k, "").strip('"') == v.strip('"') for k, v in match_dict.items()):
            result.append(d)
    return result


def resource_search(
    context: dict[str, Any], dataset_id: str = "", first_only: bool = True, **kwargs
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """
    Searches for resources in a dataset based on criteria.

    Args:
        context: CKAN context
        dataset_id: Dataset ID
        first_only: Return only first match
        **kwargs: Search criteria (e.g. name, hadPrimarySource)

    Returns:
        Matching resource(s) or None
    """
    if not dataset_id:
        return None

    try:
        dataset = toolkit.get_action("package_show")(context, {"id": dataset_id})

        # Only use non-empty values for search criteria
        match_criteria = {k: v for k, v in kwargs.items() if v}

        if not match_criteria:
            return None

        resources = dataset.get("resources", [])

        if first_only:
            return find_first_matching_dict(resources, match_criteria)
        return find_matching_dicts(resources, match_criteria)

    except Exception as e:  # pylint: disable=broad-exception-caught
        log.error("Error searching resources in dataset %s: %s", dataset_id, e)
        return None


def extract_text(
    resource: dict[str, Any],
    url: str,
    api_key: str = "",
    timeout: int = EXTRACTION_TIMEOUT,
) -> requests.Response:
    """
    Extracts text from documents via external service.

    Args:
        resource: CKAN resource dictionary
        url: Extraction service URL
        api_key: Optional API key
        timeout: Timeout in seconds

    Returns:
        requests.Response: Response from extraction service

    Raises:
        ExtractionError: On extraction errors
    """
    if not resource.get("url"):
        raise ExtractionError("Resource has no URL")

    with managed_session() as session:
        headers = prepare_headers(api_key)

        try:
            # Download resource
            log.debug("SSL_VERIFY: %s", SSL_VERIFY)
            log.debug("Downloading resource: %s", resource["url"])

            download_response = session.get(
                resource["url"], headers=headers, verify=SSL_VERIFY, timeout=timeout
            )
            download_response.raise_for_status()

            if not download_response.content:
                raise ExtractionError("Downloaded file is empty")

            # File information
            file_type = resource.get("mimetype", "application/octet-stream")
            file_data = download_response.content
            file_name = resource.get("name", "unknown_file")

            log.debug("File downloaded: %s, size: %s bytes", file_name, len(file_data))

            # Send to extraction service
            files = {"file": (file_name, file_data, file_type, {"Expires": "0"})}

            log.debug("Sending to extraction service: %s", url)
            extraction_response = session.post(url, files=files, verify=SSL_VERIFY, timeout=timeout)
            extraction_response.raise_for_status()

            log.debug(
                "Extraction successful, response size: %s bytes",
                len(extraction_response.content),
            )
            return extraction_response

        except requests.exceptions.Timeout as exc:
            raise ExtractionError(f"Extraction timeout after {timeout}s") from exc
        except requests.exceptions.HTTPError as e:
            raise ExtractionError(f"HTTP error during extraction: {e.response.status_code}") from e
        except requests.exceptions.RequestException as e:
            raise ExtractionError(f"Request error during extraction: {e}") from e


def download_file(
    url: str, api_key: str = "", timeout: int = DOWNLOAD_TIMEOUT
) -> tuple[str, str | bytes]:
    """
    Downloads a file (local or remote).

    Args:
        url: File URL (file:// or http(s)://)
        api_key: Optional API key
        timeout: Timeout for HTTP requests

    Returns:
        Tuple: (filename, content)

    Raises:
        DownloadError: On download errors
    """
    try:
        if url.startswith("file://"):
            # Local file
            path = urlparse(url).path
            filename = os.path.basename(unquote(path))

            if not os.path.exists(path):
                raise DownloadError(f"Local file not found: {path}")

            with open(path, "rb") as f:
                content = f.read()

            return filename, content

        # Remote file
        with managed_session() as session:
            headers = prepare_headers(api_key)

            response = session.get(
                url,
                headers=headers,
                stream=True,
                verify=SSL_VERIFY,
                timeout=timeout,
            )
            response.raise_for_status()

            # Extract filename from headers or URL
            filename = "downloaded_file"
            if "Content-Disposition" in response.headers:
                disposition = response.headers["Content-Disposition"]
                if "filename=" in disposition:
                    filename = disposition.split("filename=")[-1].strip("\"'")
            else:
                filename = os.path.basename(urlparse(url).path) or "downloaded_file"

            # Check content type for text files
            content_type = response.headers.get("content-type", "").lower()
            if "text/" in content_type or "json" in content_type:
                return filename, response.text
            return filename, response.content

    except requests.exceptions.Timeout as exc:
        raise DownloadError(f"Download timeout after {timeout}s for {url}") from exc
    except requests.exceptions.RequestException as e:
        raise DownloadError(f"Request error downloading {url}: {e}") from e
    except OSError as e:
        raise DownloadError(f"Filesystem error: {e}") from e


def create_field_storage(filename: str, filedata_io: BytesIO, mime_type: str) -> FileStorage:
    """
    Creates a Flask FileStorage object.

    Args:
        filename: Filename
        filedata_io: File content as BytesIO
        mime_type: MIME type

    Returns:
        FileStorage object
    """
    return FileStorage(stream=filedata_io, filename=filename, content_type=mime_type)


def file_upload(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    dataset_id: str,
    filename: str,
    filedata: BytesIO | bytes,
    res_id: str | None = None,
    extras: dict[str, Any] | None = None,
    mime_type: str = "text/csv",
    timeout: int = UPLOAD_TIMEOUT,  # pylint: disable=unused-argument
) -> dict[str, Any]:
    """
    Uploads a file as CKAN resource.

    Args:
        dataset_id: Dataset ID
        filename: Filename
        filedata: File content
        res_id: Optional existing resource ID for update
        extras: Additional metadata
        mime_type: MIME type
        timeout: Upload timeout

    Returns:
        CKAN resource dictionary

    Raises:
        UploadError: On upload errors
    """
    if not dataset_id:
        raise UploadError("Dataset ID is required")

    if not filename:
        raise UploadError("Filename is required")

    try:
        log.debug("Upload call: %s", filename)

        # Create BytesIO if bytes were passed
        if isinstance(filedata, bytes):
            filedata = BytesIO(filedata)
        elif not isinstance(filedata, BytesIO):
            raise UploadError("filedata must be BytesIO or bytes")

        # Create FileStorage
        file_storage = create_field_storage(filename, filedata, mime_type)

        # Prepare upload data
        data_dict = {
            "package_id": dataset_id,
            "name": filename,
            "upload": file_storage,
        }

        # Add extras
        if extras:
            data_dict.update(extras)

        # Upload or update
        context = {"ignore_auth": True}

        if res_id:
            data_dict["id"] = res_id
            log.debug("Resource patch: %s", filename)
            result = toolkit.get_action("resource_patch")(context, data_dict)
        else:
            log.debug("Resource create: %s", filename)
            result = toolkit.get_action("resource_create")(context, data_dict)

        log.debug("File %s uploaded: %s", filename, result.get("id", "unknown ID"))
        return result

    except Exception as e:
        raise UploadError(f"Upload error for {filename}: {e}") from e


def expand_url(base: str, url: str) -> str:
    """
    Expands relative URLs to absolute URLs.

    Args:
        base: Base URL
        url: URL to expand

    Returns:
        Absolute URL
    """
    try:
        parsed_url = urlparse(url)
        if parsed_url.scheme in ["https", "http"]:
            return url
        # Relative URL
        return urljoin(base, url)
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.warning("Error expanding URL: %s", e)
        return url


# Embedding functions (stub implementations with better structure)
def embedding_upload(  # pylint: disable=unused-argument,fixme
    resource: dict[str, Any],
    url: str,
    api_key: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> bool:
    """
    Uploads embeddings for a resource (stub implementation).

    Args:
        resource: CKAN resource
        url: Upload URL
        api_key: API key
        timeout: Timeout

    Returns:
        bool: True on success

    Raises:
        NotImplementedError: Function not yet implemented
    """
    # TODO: Implementation for embedding upload
    log.warning("embedding_upload is a stub implementation")

    with managed_session() as session:
        headers = prepare_headers(api_key)

        try:
            response = session.get(
                resource["url"], headers=headers, verify=SSL_VERIFY, timeout=timeout
            )
            response.raise_for_status()

            # Actual embedding logic would go here
            log.debug("Embedding upload for %s simulated", resource["name"])
            return True

        except requests.exceptions.RequestException as e:
            log.error("Embedding upload error: %s", e)
            return False


def embedding_exists(  # pylint: disable=unused-argument,fixme
    resource_id: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> bool:
    """
    Checks if embeddings exist for a resource (stub implementation).

    Args:
        resource_id: Resource ID
        timeout: Timeout

    Returns:
        bool: True if embeddings exist
    """
    # TODO: Implementation for embedding existence check
    log.warning("embedding_exists is a stub implementation")
    return False


def get_embedding(  # pylint: disable=unused-argument,fixme
    graph_id: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> str | bool:
    """
    Gets embedding URL for a graph ID (stub implementation).

    Args:
        graph_id: Graph ID
        timeout: Timeout

    Returns:
        URL string or False on error
    """
    # TODO: Implementation for embedding retrieval
    log.warning("get_embedding is a stub implementation")
    return False


def embedding_create(  # pylint: disable=unused-argument,fixme
    graph_id: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """
    Creates embeddings for a graph ID (stub implementation).

    Args:
        graph_id: Graph ID
        timeout: Timeout

    Returns:
        URL to created embeddings
    """
    # TODO: Implementation for embedding creation
    log.warning("embedding_create is a stub implementation")
    return f"https://example.com/embeddings/{graph_id}"
