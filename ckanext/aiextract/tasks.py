"""Background jobs for AI Extract: extraction, upload and status callbacks."""

import datetime
import io
import json
import logging
import mimetypes
import pathlib
import re
import zipfile
from contextlib import contextmanager
from typing import Any

import ckanapi
import requests
from ckan import model
from ckan.plugins import toolkit
from requests.adapters import HTTPAdapter
from rq import get_current_job
from urllib3.util.retry import Retry

from ckanext.aiextract import backend, db
from ckanext.aiextract.utils import utcnow

# Configuration constants
AIEXTRACT_CKAN_TOKEN = toolkit.config.get("ckanext.aiextract.ckan_token")
EXTRACT_URL = toolkit.config.get("ckanext.aiextract.extract_url")
SSL_VERIFY = toolkit.asbool(toolkit.config.get("ckanext.aiextract.ssl_verify", True))

# Timeout configuration
REQUEST_TIMEOUT = 300  # 5 minutes for individual requests
CALLBACK_TIMEOUT = 30  # status callbacks to CKAN must fail fast
EXTRACTION_TIMEOUT = 1800  # 30 minutes for extraction
UPLOAD_TIMEOUT = 600  # 10 minutes for uploads

# Processing constants
METADATA_FILENAME_PATTERN = "_meta.json"
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
TEXT_MIME_TYPES = ["markdown", "text/plain", "text/html"]
ZIP_MIME_TYPE = "zip"
DEFAULT_NOTES = "File extracted and processed by AI Extract service"

# Retry configuration
RETRY_TOTAL = 3
RETRY_BACKOFF_FACTOR = 1
RETRY_STATUS_CODES = [429, 500, 502, 503, 504]

if not SSL_VERIFY:
    requests.packages.urllib3.disable_warnings()


class AIExtractError(Exception):
    """Base exception for AI Extract errors"""


class ExtractionTimeoutError(AIExtractError):
    """Exception for extraction timeouts"""


class UploadError(AIExtractError):
    """Exception for upload errors"""


class ConfigurationError(AIExtractError):
    """Exception for configuration errors"""


def create_session_with_timeout() -> requests.Session:
    """
    Creates a requests Session with retry strategy and timeouts.

    Returns:
        requests.Session: Configured session
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUS_CODES,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def create_callback_session() -> requests.Session:
    """
    Creates a requests Session for status callbacks to CKAN.

    Only a single retry on connection errors; no retries on read timeouts or
    HTTP error responses (e.g. 504), so a failing callback cannot block the job.

    Returns:
        requests.Session: Configured session
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=1,
        connect=1,
        read=0,
        status=0,
        status_forcelist=[],
        respect_retry_after_header=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def extract_filename(response: requests.Response) -> str:
    """
    Extract filename from response headers.

    Args:
        response: HTTP response

    Returns:
        str: Extracted filename or 'unknown' if not found
    """
    content_disposition = response.headers.get("Content-Disposition", "")
    if "filename=" in content_disposition:
        return content_disposition.split("filename=")[-1]
    return "unknown"


def replace_links_in_content(search_string: str, new_string: str, content: str) -> str:
    """
    Replaces URLs in content (supports Markdown image links and other formats).
    """
    # Combined pattern: Markdown image links OR standalone URLs
    # This captures both in one pass
    pattern = r"(!\[.*?\]\()(.*?)(\))|(\b(?:https?://|www\.)\S+)"

    def replace_match(match):
        if match.group(1):  # Markdown image link
            prefix, url, suffix = match.group(1), match.group(2), match.group(3)
            new_url = re.sub(re.escape(search_string), new_string, url, flags=re.IGNORECASE)
            return prefix + new_url + suffix
        # Standalone URL
        url = match.group(4)
        return re.sub(re.escape(search_string), new_string, url, flags=re.IGNORECASE)

    return re.sub(pattern, replace_match, content)


@contextmanager
def managed_session_and_logger(job_id: str, job_dict: dict[str, Any]):
    """
    Context Manager for session and logger management.

    Args:
        job_id: Job ID
        job_dict: Job data dictionary

    Yields:
        Tuple[session, logger]: CKAN Session and Logger
    """
    session = model.meta.create_local_session()
    handler = StoringHandler(job_id, job_dict)
    logger = logging.getLogger(job_id)

    try:
        # Logger setup
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.addHandler(logging.StreamHandler())
        logger.setLevel(logging.DEBUG)

        yield session, logger

    finally:
        # Cleanup
        logger.removeHandler(handler)
        session.close()


@contextmanager
def database_connection():
    """Context manager for database connections."""
    conn = None
    try:
        conn = db.ENGINE.connect()
        yield conn
    finally:
        if conn:
            conn.close()


def update_job_status(
    callback_url: str,
    job_dict: dict[str, Any],
    status: str,
    error_msg: str | None = None,
) -> None:
    """
    Updates job status via callback.

    Args:
        callback_url: Callback URL
        job_dict: Job data
        status: New status ('running', 'complete', 'error')
        error_msg: Optional error message
    """
    job_dict["status"] = status
    if error_msg:
        job_dict["error"] = error_msg

    try:
        callback_aiextract_hook(callback_url, api_key=AIEXTRACT_CKAN_TOKEN, job_dict=job_dict)
    except Exception as e:  # pylint: disable=broad-exception-caught
        # Log callback error, but don't stop the entire job
        logging.getLogger().error("Callback error: %s", e)


def validate_configuration() -> None:
    """
    Validates required configuration.

    Raises:
        ConfigurationError: If configuration is invalid
    """
    if not EXTRACT_URL:
        raise ConfigurationError(
            "No service for text extraction configured! "
            "Please set CKANINI__CKANEXT__AIEXTRACT_EXTRACT_URL"
        )


def load_resource(res_id: str) -> dict[str, Any]:
    """
    Load resource by ID.

    Args:
        res_id: Resource ID

    Returns:
        Dict: Resource data

    Raises:
        AIExtractError: If resource cannot be loaded
    """
    try:
        return toolkit.get_action("resource_show")({"ignore_auth": True}, {"id": res_id})
    except Exception as e:
        raise AIExtractError(f"Error loading resource {res_id}: {e}") from e


def create_standard_resource_metadata(
    resource: dict[str, Any], additional_metadata: dict[str, Any] | None = None
) -> dict[str, str]:
    """
    Create standard metadata for all resources created by AI Extract.

    Args:
        resource: Original source resource
        additional_metadata: Optional additional metadata

    Returns:
        Dict: Standard metadata fields
    """
    metadata = {
        "hadPrimarySource": str(resource["url"]),
        "wasGeneratedBy": EXTRACT_URL,
        "notes": DEFAULT_NOTES,
        "created_by": "aiextract_service",
    }

    if additional_metadata:
        metadata.update(additional_metadata)

    return metadata


def extract_and_process_resource(
    resource: dict[str, Any], logger: logging.Logger
) -> requests.Response:
    """
    Extracts text from a resource with timeout handling.

    Args:
        resource: CKAN resource
        logger: Logger instance

    Returns:
        requests.Response: Extraction response

    Raises:
        ExtractionTimeoutError: On timeout
        AIExtractError: On other extraction errors
    """
    try:
        logger.info("Starting extraction from: %s", resource["url"])
        file_response = backend.extract_text(
            resource,
            EXTRACT_URL,
            api_key=AIEXTRACT_CKAN_TOKEN,
            timeout=EXTRACTION_TIMEOUT,
        )
        logger.debug(
            "Extraction Response: %s, Headers: %s",
            file_response.status_code,
            file_response.headers,
        )
        return file_response

    except requests.exceptions.Timeout as exc:
        raise ExtractionTimeoutError(
            f"Extraction timeout after {EXTRACTION_TIMEOUT} seconds"
        ) from exc
    except requests.exceptions.RequestException as e:
        raise AIExtractError(f"Extraction error: {e}") from e


def determine_file_type(file_response: requests.Response) -> tuple[str, str]:
    """
    Determine file type from response.

    Args:
        file_response: HTTP response

    Returns:
        Tuple[str, str]: filename and mime_type
    """
    filename = extract_filename(file_response)
    mime_type, _ = mimetypes.guess_type(filename)
    return filename, mime_type


def process_extraction_result(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    context: dict[str, Any],
    file_response: requests.Response,
    dataset_id: str,
    resource: dict[str, Any],
    callback_url: str,
    job_dict: dict[str, Any],
    logger: logging.Logger,
) -> None:
    """
    Process the extraction result based on file type.

    Args:
        context: CKAN context
        file_response: HTTP response with extracted content
        dataset_id: Dataset ID
        resource: Original resource
        callback_url: Callback URL
        job_dict: Job data
        logger: Logger
    """
    _, mime_type = determine_file_type(file_response)

    if mime_type and mime_type.endswith(ZIP_MIME_TYPE):
        process_archive_file(
            context, file_response, dataset_id, resource, callback_url, job_dict, logger
        )
    else:
        process_single_file(context, file_response, dataset_id, resource, logger)


def initialize_job_data(
    dataset_url: str, dataset_id: str, res_id: str, last_updated: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Initialize job metadata and dictionary.

    Args:
        dataset_url: Dataset URL
        dataset_id: Dataset ID
        res_id: Resource ID
        last_updated: Timestamp of last update

    Returns:
        Tuple: metadata dict and job_dict
    """
    metadata = {
        "ckan_url": backend.get_site_url(),
        "pkg_id": dataset_id,
        "resource_id": res_id,
        "task_created": last_updated,
        "original_url": dataset_url,
        "files": [],
    }
    job_info = {}
    job_dict = {"metadata": metadata, "status": "running", "job_info": job_info}
    return metadata, job_dict


def perform_main_extraction_process(  # pylint: disable=R0913,R0917
    context: dict[str, Any],
    dataset_id: str,
    res_id: str,
    callback_url: str,
    job_dict: dict[str, Any],
    logger: logging.Logger,
) -> None:
    """
    Perform the main extraction process.

    Args:
        context: CKAN context
        dataset_id: Dataset ID
        res_id: Resource ID
        callback_url: Callback URL
        job_dict: Job data
        logger: Logger
    """
    # Load resource
    resource = load_resource(res_id)
    job_dict["job_info"]["files"] = [resource.get("url")]

    # Extract and process
    file_response = extract_and_process_resource(resource, logger)
    update_job_status(callback_url, job_dict, "running")

    # Process result based on file type
    process_extraction_result(
        context, file_response, dataset_id, resource, callback_url, job_dict, logger
    )


def update(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    dataset_url: str,
    dataset_id: str,
    res_id: str,
    callback_url: str,
    last_updated: str,
    skip_if_no_changes: bool = True,  # pylint: disable=unused-argument
) -> str | None:
    """
    Main function for AI extraction with improved error handling.

    Args:
        dataset_url: Dataset URL
        dataset_id: Dataset ID
        res_id: Resource ID
        callback_url: Callback URL for status updates
        last_updated: Timestamp of last update
        skip_if_no_changes: Skip if no changes

    Returns:
        Optional[str]: "error" on errors, None on success
    """
    # Initialize job data
    _, job_dict = initialize_job_data(dataset_url, dataset_id, res_id, last_updated)
    # Fallback for errors raised before the job logger is set up (e.g. in db.init()),
    # so the except blocks below never hit an unbound ``logger``.
    logger = logging.getLogger(__name__)

    try:
        job_id = get_current_job().id
        db.init()

        with managed_session_and_logger(job_id, job_dict) as (session, logger):
            context = {"session": session, "ignore_auth": True}

            # Initial callback and validation
            update_job_status(callback_url, job_dict, "running")
            validate_configuration()

            # Perform main extraction process
            perform_main_extraction_process(
                context, dataset_id, res_id, callback_url, job_dict, logger
            )

            logger.info("Job completed, results at %s", dataset_id)
            update_job_status(callback_url, job_dict, "complete")
            return None

    except (
        ExtractionTimeoutError,
        UploadError,
        AIExtractError,
        ConfigurationError,
    ) as e:
        logger.error("Processing error: %s", e)
        update_job_status(callback_url, job_dict, "error", str(e))
        return "error"
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Unexpected error: %s", e)
        update_job_status(callback_url, job_dict, "error", f"Unexpected error: {e}")
        return "error"

    finally:
        # Cleanup
        try:
            db.ENGINE.dispose()
        except Exception as e:  # pylint: disable=broad-exception-caught
            logging.getLogger().debug("Error disposing database engine: %s", e)


def create_archive_stream(file_response: requests.Response) -> io.BytesIO:
    """Create archive stream from response content."""
    return io.BytesIO(file_response.content)


def process_archive_file(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    context: dict[str, Any],
    file_response: requests.Response,
    dataset_id: str,
    resource: dict[str, Any],
    callback_url: str,
    job_dict: dict[str, Any],
    logger: logging.Logger,
) -> None:
    """
    Processes archive files (ZIP) from extraction containing multiple files.

    Args:
        context: CKAN context
        file_response: HTTP response with archive content
        dataset_id: Dataset ID
        resource: Original resource
        callback_url: Callback URL
        job_dict: Job data
        logger: Logger
    """
    logger.info("Archive file detected, extracting contents...")
    update_job_status(callback_url, job_dict, "running")

    archive_stream = create_archive_stream(file_response)

    with zipfile.ZipFile(archive_stream, "r") as archive_file:
        # Upload all archive contents
        path_map = upload_archive_contents(context, archive_file, dataset_id, resource, logger)
        linkmap = [(k, v["res"]["url"]) for k, v in path_map.items()]
        logger.info("Extracted and uploaded files:\n %s", linkmap)

        # Process metadata if present
        metadata = process_metadata_from_archive(path_map, resource, logger)
        metadata["files"] = linkmap

        # Update metadata file if it exists
        update_metadata_in_archive(path_map, metadata, dataset_id, resource, logger)

        # Process all text files for link replacement
        process_text_files_in_archive(
            archive_file, path_map, linkmap, dataset_id, metadata, resource, logger
        )


def find_metadata_file(path_map: dict[str, Any]) -> str | None:
    """Find metadata file in path map."""
    return next((key for key in path_map if METADATA_FILENAME_PATTERN in key), None)


def load_metadata_from_file(meta_res: dict[str, Any], logger: logging.Logger) -> dict[str, Any]:
    """Load metadata from file resource."""
    try:
        _, metafile_data = backend.download_file(
            meta_res["url"], api_key=AIEXTRACT_CKAN_TOKEN, timeout=REQUEST_TIMEOUT
        )
        return json.loads(metafile_data)
    except (json.JSONDecodeError, requests.RequestException) as e:
        logger.warning("Error loading metadata: %s", e)
        return {}


def enrich_metadata_with_resource_info(
    metadata: dict[str, Any], resource: dict[str, Any]
) -> dict[str, Any]:
    """Enrich metadata with resource information."""
    metadata.setdefault("title", resource.get("title") or backend.get_title(metadata))
    metadata.setdefault("authors", resource.get("author") or backend.get_authors(metadata))
    metadata.setdefault(
        "publisher",
        resource.get("publisher") or backend.get_first_in_nested(metadata, "publisher"),
    )
    return metadata


def process_metadata_from_archive(
    path_map: dict[str, Any], resource: dict[str, Any], logger: logging.Logger
) -> dict[str, Any]:
    """
    Processes metadata file from archive if present.

    Args:
        path_map: Mapping of archive paths to resources
        resource: Original resource
        logger: Logger

    Returns:
        Dict: Metadata
    """
    meta_filename = find_metadata_file(path_map)
    metadata = {}

    if meta_filename:
        meta_res = path_map[meta_filename]["res"]
        logger.debug("Metadata file found: %s", meta_res["name"])
        metadata = load_metadata_from_file(meta_res, logger)

    return enrich_metadata_with_resource_info(metadata, resource)


def update_metadata_in_archive(
    path_map: dict[str, Any],
    metadata: dict[str, Any],
    dataset_id: str,
    resource: dict[str, Any],
    logger: logging.Logger,
) -> None:
    """
    Updates the metadata file in archive if present.

    Args:
        path_map: Path mapping
        metadata: Metadata
        dataset_id: Dataset ID
        resource: Original resource
        logger: Logger
    """
    meta_filename = find_metadata_file(path_map)
    if not meta_filename:
        return

    meta_file_id = path_map[meta_filename]["res"]["id"]
    metafile_data = json.dumps(metadata, indent=4)

    try:
        backend.file_upload(
            dataset_id=dataset_id,
            filename=meta_filename,
            mime_type=path_map[meta_filename]["res"]["mimetype"],
            filedata=io.BytesIO(metafile_data.encode()),
            res_id=meta_file_id,
            extras=create_standard_resource_metadata(resource),
            timeout=UPLOAD_TIMEOUT,
        )
        logger.info("Metadata updated in file: %s", meta_filename)
    except Exception as e:
        raise UploadError(f"Error uploading metadata: {e}") from e


def is_text_file(mimetype: str) -> bool:
    """Check if file is a text file that may contain links."""
    if not mimetype:
        return False
    return any(text_type in mimetype.lower() for text_type in TEXT_MIME_TYPES)


def process_single_text_file(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    archive_file: zipfile.ZipFile,
    subfilename: str,
    file_info: dict[str, Any],
    linkmap: list[tuple[str, str]],
    dataset_id: str,
    metadata: dict[str, Any],
    resource: dict[str, Any],
    logger: logging.Logger,
) -> None:
    """Process a single text file from archive."""
    try:
        file_stream = archive_file.read(subfilename)
        file_content = file_stream.decode()

        # Replace all links in content
        for from_, to in linkmap:
            if not from_.endswith((".md", ".json")):
                file_content = replace_links_in_content(from_, to, file_content)

        # Create enhanced metadata for text files
        enhanced_metadata = create_standard_resource_metadata(
            resource,
            {
                "title": metadata.get("title"),
                "author": metadata.get("authors"),
                "publisher_name": metadata.get("publisher"),
            },
        )

        # Upload file
        backend.file_upload(
            dataset_id=dataset_id,
            filename=subfilename,
            filedata=io.BytesIO(file_content.encode()),
            mime_type=file_info["mimetype"],
            res_id=file_info["res"]["id"],
            extras=enhanced_metadata,
            timeout=UPLOAD_TIMEOUT,
        )
        logger.info("Processed text file: %s", subfilename)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Error processing text file %s: %s", subfilename, e)


def process_text_files_in_archive(  # pylint: disable=R0913,R0917
    archive_file: zipfile.ZipFile,
    path_map: dict[str, Any],
    linkmap: list[tuple[str, str]],
    dataset_id: str,
    metadata: dict[str, Any],
    resource: dict[str, Any],
    logger: logging.Logger,
) -> None:
    """
    Processes all text files in archive and replaces links.

    Args:
        archive_file: Archive file (ZIP)
        path_map: Path mapping
        linkmap: Link mapping for replacements
        dataset_id: Dataset ID
        metadata: Metadata
        resource: Original resource
        logger: Logger
    """
    for subfilename, file_info in path_map.items():
        if is_text_file(file_info["mimetype"]):
            logger.info("Text file found: %s", file_info["res"]["name"])
            process_single_text_file(
                archive_file,
                subfilename,
                file_info,
                linkmap,
                dataset_id,
                metadata,
                resource,
                logger,
            )


def process_single_file(
    context: dict[str, Any],
    file_response: requests.Response,
    dataset_id: str,
    resource: dict[str, Any],
    logger: logging.Logger,
) -> None:
    """
    Processes single (non-archive) files.

    Args:
        context: CKAN context
        file_response: HTTP response
        dataset_id: Dataset ID
        resource: Original resource
        logger: Logger
    """
    filename = extract_filename(file_response)
    mime_type, _ = mimetypes.guess_type(filename)

    # Check if resource already exists
    existing_res = backend.resource_search(
        context, dataset_id, name=filename, hadPrimarySource=resource["url"]
    )
    res_id = existing_res["id"] if existing_res else None

    try:
        backend.file_upload(
            dataset_id=dataset_id,
            filename=filename,
            filedata=file_response.content,
            res_id=res_id,
            mime_type=mime_type,
            extras=create_standard_resource_metadata(resource),
            timeout=UPLOAD_TIMEOUT,
        )
        logger.info("Upload %s to dataset %s successful", filename, dataset_id)

    except Exception as e:
        raise UploadError(f"Error uploading {filename}: {e}") from e


def generate_filename(source_name: str, path: pathlib.Path) -> str:
    """Generate filename for archive contents."""
    return f"{source_name}_{path.name}" if source_name not in path.name else path.name


def upload_single_archive_file(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    context: dict[str, Any],
    archive_file: zipfile.ZipFile,
    file_info: zipfile.ZipInfo,
    dataset_id: str,
    source_res: dict[str, Any],
    source_name: str,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    """Upload a single file from archive contents."""
    try:
        file_stream = io.BytesIO(archive_file.read(file_info.filename))
        p = pathlib.Path(file_info.filename)
        filename = generate_filename(source_name, p)
        mimetype, _ = mimetypes.guess_type(p.name)

        # Check existing resource
        existing_res = backend.resource_search(
            context, dataset_id, name=filename, hadPrimarySource=source_res["url"]
        )
        res_id = existing_res["id"] if existing_res else None

        # Upload with standard metadata
        res_up = backend.file_upload(
            dataset_id=dataset_id,
            filename=filename,
            filedata=file_stream,
            res_id=res_id,
            mime_type=mimetype,
            extras=create_standard_resource_metadata(source_res),
            timeout=UPLOAD_TIMEOUT,
        )

        logger.info("Uploaded %s from archive", filename)
        return {
            "zip_path": p,
            "res": res_up,
            "mimetype": mimetype,
        }

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Error uploading %s: %s", file_info.filename, e)
        return None


def upload_archive_contents(
    context: dict[str, Any],
    archive_file: zipfile.ZipFile,
    dataset_id: str,
    source_res: dict[str, Any],
    logger: logging.Logger,
) -> dict[str, Any]:
    """
    Uploads archive contents as individual resources.

    Args:
        context: CKAN context
        archive_file: Archive file (ZIP)
        dataset_id: Dataset ID
        source_res: Source resource
        logger: Logger

    Returns:
        Dict: Mapping of filenames to resource info
    """
    result = {}
    source_name = source_res["name"].split(".")[0]

    for file_info in archive_file.filelist:
        upload_result = upload_single_archive_file(
            context,
            archive_file,
            file_info,
            dataset_id,
            source_res,
            source_name,
            logger,
        )
        if upload_result:
            result[file_info.filename] = upload_result

    return result


# Utility functions
def get_resource(resource_id: str) -> dict[str, Any]:
    """
    Get resource by ID.

    Args:
        resource_id: Resource ID

    Returns:
        Dict: Resource data

    Raises:
        AIExtractError: If resource cannot be found
    """
    local_ckan = ckanapi.LocalCKAN()
    try:
        return local_ckan.action.resource_show(id=resource_id)
    except Exception as e:
        raise AIExtractError(f"Resource {resource_id} not found: {e}") from e


def callback_aiextract_hook(result_url: str, api_key: str, job_dict: dict[str, Any]) -> bool:
    """
    Sends job status to CKAN callback with timeout.

    Args:
        result_url: Callback URL
        api_key: API key
        job_dict: Job data

    Returns:
        bool: True on success
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        header, key = ("Authorization", api_key) if ":" not in api_key else api_key.split(":", 1)
        headers[header] = key

    try:
        with create_callback_session() as session:
            result = session.post(
                result_url,
                data=json.dumps(job_dict, cls=DatetimeJsonEncoder),
                verify=SSL_VERIFY,
                headers=headers,
                timeout=CALLBACK_TIMEOUT,
            )
        if result.status_code != requests.codes.ok:
            logging.getLogger().error(
                "Callback to %s failed with HTTP %s", result_url, result.status_code
            )
        return result.status_code == requests.codes.ok

    except requests.exceptions.RequestException as e:
        logging.getLogger().error("Callback error: %s", e)
        return False


class StoringHandler(logging.Handler):
    """Handler for storing log records in database."""

    def __init__(self, task_id: str, input_data: dict[str, Any]):
        super().__init__()
        self.task_id = task_id
        self.input = input_data

    def emit(self, record: logging.LogRecord) -> None:
        """Store log record in database with error handling."""
        try:
            with database_connection() as conn:
                conn.execute(
                    db.LOGS_TABLE.insert().values(
                        job_id=self.task_id,
                        timestamp=utcnow(),
                        message=str(record.getMessage()),
                        level=str(record.levelname),
                        module=str(record.module),
                        funcName=str(record.funcName),
                        lineno=record.lineno,
                    )
                )
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Logging errors should not stop the main process
            print(f"Error saving log: {e}")


class DatetimeJsonEncoder(json.JSONEncoder):
    """Custom JSON encoder for datetime objects."""

    def default(self, o: Any) -> Any:
        if isinstance(o, datetime.datetime):
            return o.isoformat()
        return super().default(o)
