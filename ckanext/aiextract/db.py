"""
Abstracts a database. Used for storing logging when it aiextract a resource into
DataStore.

Loosely based on ckan-service-provider's db.py
"""

import datetime
import json

import sqlalchemy
from ckan.plugins import toolkit

ENGINE = None
_METADATA = None
JOBS_TABLE = None
METADATA_TABLE = None
LOGS_TABLE = None

DB_URL = toolkit.config.get("ckanext.aiextract.db_url", "sqlite:////tmp/jobs.db")


def init(db_uri=DB_URL, echo=False):
    """Initialise the database."""
    global ENGINE, _METADATA, JOBS_TABLE, METADATA_TABLE, LOGS_TABLE  # pylint: disable=W0603
    ENGINE = sqlalchemy.create_engine(db_uri, echo=echo, convert_unicode=True)
    _METADATA = sqlalchemy.MetaData(ENGINE)
    JOBS_TABLE = _init_jobs_table()
    METADATA_TABLE = _init_metadata_table()
    LOGS_TABLE = _init_logs_table()
    _METADATA.create_all(ENGINE)


def drop_all():
    """Delete all the database tables (if they exist)."""
    if _METADATA:
        _METADATA.drop_all(ENGINE)


def get_job(job_id):
    """Return the job with the given job_id as a dict."""
    if job_id:
        job_id = str(job_id)

    with ENGINE.connect() as conn:
        result = conn.execute(JOBS_TABLE.select().where(JOBS_TABLE.c.job_id == job_id)).first()

    if not result:
        return None

    result_dict = {}
    for field in list(result.keys()):
        value = getattr(result, field)
        if value is None:
            result_dict[field] = value
        elif field in ("sent_data", "data", "error"):
            result_dict[field] = json.loads(value)
        elif isinstance(value, datetime.datetime):
            result_dict[field] = value.isoformat()
        else:
            result_dict[field] = str(value)

    result_dict["metadata"] = _get_metadata(job_id)
    result_dict["logs"] = _get_logs(job_id)

    return result_dict


def add_pending_job(job_id, job_type, data=None, metadata=None, result_url=None):
    """Add a new job with status "pending" to the jobs table."""
    if not data:
        data = {}
    data = json.dumps(data)

    if job_id:
        job_id = str(job_id)
    if job_type:
        job_type = str(job_type)
    if result_url:
        result_url = str(result_url)
    data = str(data)

    if not metadata:
        metadata = {}

    with ENGINE.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                JOBS_TABLE.insert().values(
                    job_id=job_id,
                    job_type=job_type,
                    status="pending",
                    requested_timestamp=datetime.datetime.utcnow(),
                    sent_data=data,
                    result_url=result_url,
                )
            )

            inserts = []
            for key, value in list(metadata.items()):
                type_ = "string"
                if not isinstance(value, str):
                    value = json.dumps(value)
                    type_ = "json"

                key = str(key)
                value = str(value)

                inserts.append({"job_id": job_id, "key": key, "value": value, "type": type_})
            if inserts:
                conn.execute(METADATA_TABLE.insert(), inserts)
            trans.commit()
        except Exception:
            trans.rollback()
            raise


def delete_job(job_id):
    """Delete a job from the jobs table by job_id.

    :param job_id: the job_id of the job to be deleted
    :type job_id: unicode
    """
    if job_id:
        job_id = str(job_id)

    msg = ""
    with ENGINE.connect() as conn:
        trans = conn.begin()
        try:
            result = conn.execute(JOBS_TABLE.delete().where(JOBS_TABLE.c.job_id == job_id))
            if result.rowcount == 0:
                msg = f"No job found with id: {job_id}"
            else:
                msg = f"Job with id: {job_id} has been deleted successfully."
            trans.commit()
        except Exception as e:  # pylint: disable=broad-exception-caught
            trans.rollback()
            msg = f"An error occurred: {e}"
    return msg


class InvalidErrorObjectError(Exception):
    """Raised when a job error object is neither a string nor a dict with a string "message"."""


def _validate_error(error):
    """Validate and return the given error object.

    Based on the given error object, return either None or a dict with a
    "message" key whose value is a string (the dict may also have any other
    keys that it wants).

    The given "error" object can be:

    - None, in which case None is returned

    - A string, in which case a dict like this will be returned:
      {"message": error_string}

    - A dict with a "message" key whose value is a string, in which case the
      dict will be returned unchanged

    :param error: the error object to validate

    :raises InvalidErrorObjectError: If the error object doesn't match any of
        the allowed types

    """
    if error is None:
        return None
    if isinstance(error, str):
        return {"message": error}
    try:
        message = error["message"]
        if isinstance(message, str):
            return error
        raise InvalidErrorObjectError("error['message'] must be a string")
    except (TypeError, KeyError) as exc:
        raise InvalidErrorObjectError(
            "error must be either a string or a dict with a message key"
        ) from exc


def _update_job(job_id, job_dict):
    """Update the database row for the given job_id with the given job_dict."""
    if job_id:
        job_id = str(job_id)

    if "error" in job_dict:
        job_dict["error"] = _validate_error(job_dict["error"])
        job_dict["error"] = json.dumps(job_dict["error"])
        job_dict["error"] = str(job_dict["error"])

    if "data" in job_dict:
        job_dict["data"] = str(job_dict["data"])

    with ENGINE.connect() as conn:
        conn.execute(JOBS_TABLE.update().where(JOBS_TABLE.c.job_id == job_id).values(**job_dict))


def mark_job_as_completed(job_id, data=None):
    """Mark a job as completed successfully.

    :param job_id: the job_id of the job to be updated
    :type job_id: unicode

    :param data: the output data returned by the job
    :type data: any JSON-serializable type (including None)

    """
    update_dict = {
        "status": "complete",
        "data": json.dumps(data),
        "finished_timestamp": datetime.datetime.utcnow(),
    }
    _update_job(job_id, update_dict)


def mark_job_as_missed(job_id):
    """Mark a job as missed because it was in the queue for too long.

    :param job_id: the job_id of the job to be updated
    :type job_id: unicode

    """
    update_dict = {
        "status": "error",
        "error": "Job delayed too long, service full",
        "finished_timestamp": datetime.datetime.utcnow(),
    }
    _update_job(job_id, update_dict)


def mark_job_as_errored(job_id, error_object):
    """Mark a job as failed with an error.

    :param job_id: the job_id of the job to be updated
    :type job_id: unicode

    :param error_object: the error returned by the job
    :type error_object: either a string or a dict with a "message" key whose
        value is a string

    """
    update_dict = {
        "status": "error",
        "error": error_object,
        "finished_timestamp": datetime.datetime.utcnow(),
    }
    _update_job(job_id, update_dict)


def mark_job_as_failed_to_post_result(job_id):
    """Mark a job as 'failed to post result'.

    This happens when a job completes (either successfully or with an error)
    then trying to post the job result back to the job's callback URL fails.

    FIXME: This overwrites any error from the job itself!

    :param job_id: the job_id of the job to be updated
    :type job_id: unicode

    """
    update_dict = {
        "error": "Process completed but unable to post to result_url",
    }
    _update_job(job_id, update_dict)


def _init_jobs_table():
    """Initialise the "jobs" table in the db."""
    _jobs_table = sqlalchemy.Table(
        "jobs",
        _METADATA,
        sqlalchemy.Column("job_id", sqlalchemy.UnicodeText, primary_key=True),
        sqlalchemy.Column("job_type", sqlalchemy.UnicodeText),
        sqlalchemy.Column("status", sqlalchemy.UnicodeText, index=True),
        sqlalchemy.Column("data", sqlalchemy.UnicodeText),
        sqlalchemy.Column("error", sqlalchemy.UnicodeText),
        sqlalchemy.Column("requested_timestamp", sqlalchemy.DateTime),
        sqlalchemy.Column("finished_timestamp", sqlalchemy.DateTime),
        sqlalchemy.Column("sent_data", sqlalchemy.UnicodeText),
        # Callback URL:
        sqlalchemy.Column("result_url", sqlalchemy.UnicodeText),
    )
    return _jobs_table


def _init_metadata_table():
    """Initialise the "metadata" table in the db."""
    _metadata_table = sqlalchemy.Table(
        "metadata",
        _METADATA,
        sqlalchemy.Column(
            "job_id",
            sqlalchemy.ForeignKey("jobs.job_id", ondelete="CASCADE"),
            nullable=False,
            primary_key=True,
        ),
        sqlalchemy.Column("key", sqlalchemy.UnicodeText, primary_key=True),
        sqlalchemy.Column("value", sqlalchemy.UnicodeText, index=True),
        sqlalchemy.Column("type", sqlalchemy.UnicodeText),
    )
    return _metadata_table


def _init_logs_table():
    """Initialise the "logs" table in the db."""
    _logs_table = sqlalchemy.Table(
        "logs",
        _METADATA,
        sqlalchemy.Column(
            "job_id",
            sqlalchemy.ForeignKey("jobs.job_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sqlalchemy.Column("timestamp", sqlalchemy.DateTime),
        sqlalchemy.Column("message", sqlalchemy.UnicodeText),
        sqlalchemy.Column("level", sqlalchemy.UnicodeText),
        sqlalchemy.Column("module", sqlalchemy.UnicodeText),
        sqlalchemy.Column("funcName", sqlalchemy.UnicodeText),
        sqlalchemy.Column("lineno", sqlalchemy.Integer),
    )
    return _logs_table


def _get_metadata(job_id):
    """Return any metadata for the given job_id from the metadata table."""
    job_id = str(job_id)
    with ENGINE.connect() as conn:
        results = conn.execute(
            METADATA_TABLE.select().where(METADATA_TABLE.c.job_id == job_id)
        ).fetchall()

    metadata = {}
    for row in results:
        value = row["value"]
        if row["type"] == "json":
            value = json.loads(value)
        metadata[row["key"]] = value
    return metadata


def _get_logs(job_id):
    """Return any logs for the given job_id from the logs table."""
    job_id = str(job_id)
    with ENGINE.connect() as conn:
        results = conn.execute(LOGS_TABLE.select().where(LOGS_TABLE.c.job_id == job_id)).fetchall()

    results = [dict(result) for result in results]

    for result in results:
        result.pop("job_id")

    return results
