"""CKAN action functions of ckanext-aiextract.

Provides actions to enqueue, delete and track AI extraction background jobs.
"""

import datetime
import json
import logging
from typing import Any

import sqlalchemy as sa
from ckan import model
from ckan.lib.jobs import DEFAULT_QUEUE_NAME
from ckan.plugins import toolkit
from dateutil.parser import isoparse as parse_iso_date
from dateutil.parser import parse as parse_date

from ckanext.aiextract import backend, db
from ckanext.aiextract.tasks import update
from ckanext.aiextract.utils import utcnow

if toolkit.check_ckan_version("2.10"):
    from ckan.types import Context  # pylint: disable=ungrouped-imports
else:

    class Context(dict):
        """Fallback Context type for CKAN versions older than 2.10."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)


JOB_TIMEOUT = int(toolkit.config.get("ckanext.aiextract.job_timeout", 1800))


DEFAULT_FORMATS = toolkit.config.get("ckanext.aiextract.formats", "").lower().split()
if not DEFAULT_FORMATS:
    DEFAULT_FORMATS = [
        "pdf",
    ]


log = logging.getLogger(__name__)

# Context flag: the action runs inside the commit of ``model.Session`` (CKAN calls
# IResourceUrlChange.notify from its before_commit hook). ``model.Session`` must then
# not be committed again; task_status writes are persisted by the enclosing commit.
WITHIN_COMMIT = "aiextract_within_commit"

TASK_STATUS_COLUMNS = (
    "entity_id",
    "entity_type",
    "task_type",
    "key",
    "value",
    "state",
    "error",
    "last_updated",
)


def _save_task_status(task: dict[str, Any], commit: bool = True) -> None:
    """Insert or overwrite the task_status row of ``task`` using ``model.Session`` only.

    CKAN core ``task_status_update`` reads via ``model.Session`` but writes via
    ``context["session"]`` and always commits and closes that session, which is unsafe
    inside a running commit. This writes the row once, in the request's own session, so
    the request can never wait on a lock held by itself.

    Args:
        task (dict): task_status fields; the row is looked up by its unique key
            (entity_id, task_type, key).
        commit (bool): Commit ``model.Session`` afterwards. Must be False while
            ``model.Session`` is committing (the enclosing commit persists the row).
    """
    session = model.Session
    task_status = (
        session.query(model.TaskStatus)
        .filter_by(entity_id=task["entity_id"], task_type=task["task_type"], key=task["key"])
        .first()
    )
    if task_status is None:
        task_status = model.TaskStatus()
    for column in TASK_STATUS_COLUMNS:
        if column in task:
            setattr(task_status, column, task[column])
    session.add(task_status)
    if commit:
        try:
            session.commit()
        except sa.exc.SQLAlchemyError:
            session.rollback()
            raise


def ready_for_task(  # pylint: disable=unused-argument
    context: Context, res_id: str, status: dict
) -> bool:
    """Check whether a new aiextract task may be started for a resource.

    Read-only on ``task_status``: a stale or completed task is not deleted here, it is
    overwritten by :func:`enqueue_update` (one write per row and request).

    Args:
        context (Context): CKAN Context (kept for API compatibility).
        res_id (str): The resource ID the task belongs to.
        status (dict): Status dictionary containing job info.
    Returns:
        bool: False if a not completed task younger than the job timeout exists, else True.
    """
    try:
        existing_task = toolkit.get_action("task_status_show")(
            {},
            {
                "entity_id": res_id,
                "task_type": "aiextract",
                "key": "aiextract",
            },
        )
    except Exception:  # pylint: disable=broad-exception-caught
        existing_task = None
    log.debug(existing_task)
    assume_task_stale_after = datetime.timedelta(seconds=JOB_TIMEOUT)
    if existing_task and existing_task.get("state") != "complete":
        updated = parse_iso_date(existing_task["last_updated"])
        if updated.tzinfo is None:
            # task_status.last_updated is stored as naive UTC (see utils.utcnow)
            updated = updated.replace(tzinfo=datetime.timezone.utc)
        updated = updated.astimezone(datetime.timezone.utc)
        time_since_last_updated = datetime.datetime.now(datetime.timezone.utc) - updated

        log.info("%s ?<> %s", time_since_last_updated, assume_task_stale_after)

        if time_since_last_updated < assume_task_stale_after:
            log.info(
                "A pending task was found %s, and it is %s old. Limit is %s.",
                existing_task["id"],
                time_since_last_updated,
                assume_task_stale_after,
            )
            return False  # pending task not timeouted, not ready
        log.info(
            "An old pending task was found %s for this resource, it will be overwritten",
            existing_task["id"],
        )
    # Stale or completed tasks are not deleted (task_status_delete commits model.Session
    # in the middle of the request); enqueue_update overwrites the row instead.
    if status.get("job_id", None):
        if db.get_job(status["job_id"]):
            log.debug(
                "Trying to delete job with id %s from AIembeddings database", status["job_id"]
            )
            db.delete_job(status["job_id"])
    log.debug("No Active task")
    return True  # no activ task


def aiextract_delete(context: Context, data_dict: dict[str, Any]) -> dict[str, Any]:
    """Delete the accompanying Aiextract resources

    Args:
        context (Context): CKAN Contaxt that is passed to authorization and action
            functions containing some computed variables.
        data_dict (dict): Dict contains any data posted by the user to CKAN, eg. any fields
            they’ve completed in a web form they’re submitting or any JSON fields they’ve
            posted to the API.

    Returns:
        _type_: Resource Id the aiextract graph is delete for if succesfull, or empty dict if not.
    """
    toolkit.check_access("aiextract_delete", context, data_dict)

    dataset_id = toolkit.get_or_bust(data_dict, "pkg_id")
    try:
        pkg_dict = toolkit.get_action("package_show")(context, {"id": dataset_id})
    except (toolkit.ObjectNotFound, toolkit.NotAuthorized):
        log.error("cant get package with id: %s, maybe not authorized?", dataset_id)
        return False
    res_ids = toolkit.get_or_bust(data_dict, "resource_ids")
    pdf_ressources = [res for res in pkg_dict["resources"] if res["id"] in res_ids]
    all_ready = True
    for pdf in pdf_ressources:
        status = toolkit.get_action("aiextract_update_status")(context, {"pdfs": [pdf]})
        if not ready_for_task(context, pdf["id"], status):
            all_ready = False
            continue
        if pdf.get("url"):
            resources = backend.resource_search(
                context, dataset_id, first_only=False, hadPrimarySource=pdf["url"]
            )
            for resource in resources or []:
                toolkit.get_action("resource_delete")(context, resource)
    return all_ready


def aiextract_update(context: Context, data_dict: dict[str, Any]) -> dict[str, Any]:
    """Starts an Update Task accompanying Aiextract Dataset

    Args:
        context (Context): CKAN Contaxt that is passed to authorization and action
            functions containing some computed variables.
        data_dict (dict): Dict contains any data posted by the user to CKAN, eg. any fields
            they’ve completed in a web form they’re submitting or any JSON fields they’ve
            posted to the API.

    Returns:
        dict[str, Any]: The resource the update taske is started for
    """

    toolkit.check_access("aiextract_update", context, data_dict)

    dataset_id = toolkit.get_or_bust(data_dict, "pkg_id")
    try:
        pkg_dict = toolkit.get_action("package_show")(context, {"id": dataset_id})
    except (toolkit.ObjectNotFound, toolkit.NotAuthorized):
        log.error("cant get package with id: %s, maybe not authorized?", dataset_id)
        return False

    res_ids = toolkit.get_or_bust(data_dict, "resource_ids")
    pdf_ressources = [res for res in pkg_dict["resources"] if res["id"] in res_ids]
    log.debug("aiextract_update started for dataset: %s and resources: %s", dataset_id, res_ids)
    res = []
    for res_id in res_ids:
        status = toolkit.get_action("aiextract_update_status")(
            context, {"pdfs": [r for r in pdf_ressources if r["id"] == res_id]}
        )
        if ready_for_task(context, res_id, status):
            res.append(
                enqueue_update(
                    context,
                    pkg_dict["name"],
                    pkg_dict["id"],
                    res_id,
                    operation="changed",
                )
            )
    log.debug("enqueue job: %s", res)
    return any(res)


def enqueue_update(  # pylint: disable=R0913,R0917,R0914
    context: Context,
    dataset_url: str,
    dataset_id: str,
    res_id: str,
    operation: str,
) -> bool:
    """Enquery a Update Task as Background Job

    The task_status row of the resource is written once, via ``model.Session``; an
    existing (stale or completed) task is overwritten. If ``context[WITHIN_COMMIT]`` is
    set, ``model.Session`` is not committed here (the enclosing commit persists the row).

    Args:
        context (Context): CKAN context.
        dataset_url (str): Url of the dataset
        dataset_id (str): Dateset Id the ressource is associated with
        res_ids (list): Ids of the ressources to use
        operation (str): a string discribing what has trigged the tasks, like update, create

    Raises:
        Exception: Object not found

    Returns:
        bool: True if the the update job was successful enqueued.
    """

    # skip task if the dataset is already queued
    queue = DEFAULT_QUEUE_NAME
    # Check if this resource is already in the process of being xloadered
    task = {
        "entity_id": res_id,
        "entity_type": "resource",
        "task_type": "aiextract",
        "last_updated": str(utcnow()),
        "state": "submitting",
        "key": "aiextract",
        "value": "{}",
        "error": "{}",
        "detail": "",
    }
    try:
        existing_task = toolkit.get_action("task_status_show")(
            # context,
            {"ignore_auth": True},
            {"entity_id": res_id, "task_type": "aiextract", "key": "aiextract"},
        )
        assume_task_stale_after = datetime.timedelta(seconds=JOB_TIMEOUT)
        if existing_task.get("state") == "pending":
            updated = parse_iso_date(existing_task["last_updated"])
            time_since_last_updated = utcnow() - updated
            log.info("%s ?<> %s", time_since_last_updated, assume_task_stale_after)
            if time_since_last_updated > assume_task_stale_after:
                log.info(
                    "A pending task was found %r, but it is only %s hours old",
                    existing_task["id"],
                    time_since_last_updated,
                )
            else:
                log.info(
                    "A pending task was found %s for this resource, so "
                    "skipping this duplicate task",
                    existing_task["id"],
                )
                return False

        task["id"] = existing_task["id"]
    except toolkit.ObjectNotFound:
        pass

    # ckanext.aiextract.callback_base_url (default: ckan.site_url) + path incl. root_path
    callback_url = backend.get_ckan_base_url() + toolkit.url_for(
        "api.action", ver=3, logic_function="aiextract_hook"
    )

    # add this dataset to the queue
    job = toolkit.enqueue_job(
        update,
        [dataset_url, dataset_id, res_id, callback_url, task["last_updated"]],
        title=f'aiextract {operation} "{dataset_id}" {dataset_url}',
        queue=queue,  # , timeout=JOB_TIMEOUT
    )
    # Store details of the job in the db
    try:
        db.add_pending_job(
            job.id,
            job_type=task["task_type"],
            metadata={"res_id": res_id},
            result_url=callback_url,
        )
    except sa.exc.IntegrityError as exc:
        raise Exception(  # pylint: disable=broad-exception-raised
            f"job_id {task['id']} already exists"
        ) from exc

    log.debug("Enqueued job %s to %s resource %s", job.id, operation, dataset_id)

    value = json.dumps({"job_id": job.id})
    task["value"] = value
    task["state"] = "pending"
    task["last_updated"] = str(utcnow())
    _save_task_status(task, commit=not context.get(WITHIN_COMMIT))

    return True


def aiextract_hook(context: Context, data_dict: dict[str, Any]):
    """Update Aiextract Task Status called by backgroundjob running to update job information.

    Args:
        context (Context): CKAN Contaxt that is passed to authorization and action
            functions containing some computed variables.
        data_dict (dict): Dict contains any data posted by the user to CKAN, eg. any fields
            they’ve completed in a web form they’re submitting or any JSON fields they’ve
            posted to the API.
            Must include 'metadata', 'status', 'job_info' key values

    """

    metadata, status, job_info = toolkit.get_or_bust(data_dict, ["metadata", "status", "job_info"])

    res_id = toolkit.get_or_bust(metadata, "resource_id")
    pkg_id = toolkit.get_or_bust(metadata, "pkg_id")

    # Pass metadata, not data_dict, as it contains the resource id needed
    # on the auth checks
    # toolkit.check_access('xloader_submit', context, metadata)

    task = toolkit.get_action("task_status_show")(
        context, {"entity_id": res_id, "task_type": "aiextract", "key": "aiextract"}
    )

    task["state"] = status
    task["last_updated"] = str(utcnow())
    task["error"] = data_dict.get("error")
    task["task_info"] = job_info
    resubmit = False
    log.debug("task update for entity_id %s with: %s", res_id, task)
    if status in ("complete", "running_but_viewable"):
        # Create default views for resource if necessary (only the ones that
        # require data to be in the DataStore)
        # resource_dict = toolkit.get_action('resource_show')(
        #     context, {'id': pkg_id})

        res_dict = toolkit.get_action("resource_show")(context, {"id": res_id})

        # Check if the uploaded file has been modified in the meantime
        if res_dict.get("last_modified") and metadata.get("task_created"):
            try:
                last_modified_datetime = parse_date(res_dict["last_modified"])
                task_created_datetime = parse_date(metadata["task_created"])
                if last_modified_datetime > task_created_datetime:
                    log.debug(
                        "Uploaded file more recent: %s > %s",
                        last_modified_datetime,
                        task_created_datetime,
                    )
                    resubmit = True
            except ValueError:
                pass
        # Check if the URL of the file has been modified in the meantime
        elif (
            res_dict.get("url")
            and metadata.get("original_url")
            and res_dict["url"] != metadata["original_url"]
        ):
            log.debug(
                "URLs are different: %s != %s",
                res_dict["url"],
                metadata["original_url"],
            )
            resubmit = True
        # mark job completed in db
        log.debug(task)

        if status == "complete":
            log.debug("job complete now update job db at: %s", task)
            job_id = json.loads(task["value"])["job_id"]
            db.mark_job_as_completed(job_id, data=job_info)

    context["ignore_auth"] = True
    toolkit.get_action("task_status_update")(context, task)

    if resubmit:
        log.debug("Resource %s has been modified, resubmitting to aiextract", res_id)
        toolkit.get_action("aiextract_update")(
            context,
            {
                "pkg_id": pkg_id,
                "resource_ids": [
                    res_id,
                ],
            },
        )


@toolkit.side_effect_free
def aiextract_update_status(context: Context, data_dict: dict[str, Any]) -> dict[str, Any]:
    """Get the status of a the transformation job for a certain resource.

    Args:
    context (Context): CKAN Contaxt that is passed to authorization and action
        functions containing some computed variables.
    data_dict (dict): Dict contains any data posted by the user to CKAN, eg. any fields
        they’ve completed in a web form they’re submitting or any JSON fields they’ve
        posted to the API.
        Must include 'resource_id' as string

    """
    toolkit.check_access("aiextract_update_status", context, data_dict)
    # pdfs = toolkit.get_or_bust(data_dict, "pdfs")
    pdfs = data_dict.get("pdfs", [])
    job_id = None
    status = {"tasks": {}}
    try:
        for pdf in pdfs:
            task = toolkit.get_action("task_status_show")(
                context,
                {"entity_id": pdf["id"], "task_type": "aiextract", "key": "aiextract"},
            )
            value = json.loads(task["value"])
            job_id = value.get("job_id")
            url = None
            try:
                error = json.loads(task["error"])
            except ValueError:
                # this happens occasionally, such as when the job times out
                error = task["error"]
            status["tasks"][job_id] = {
                "status": task["state"],
                "job_id": job_id,
                "job_url": url,
                "last_updated": task["last_updated"],
                "error": error,
                "info": str(task.keys()),
            }
            if job_id:
                # get logs from db
                db_job = db.get_job(job_id)

                if db_job and db_job.get("logs"):
                    # for log in db_job["logs"]:
                    #     if "timestamp" in log and isinstance(
                    #         log["timestamp"], datetime.datetime
                    #     ):
                    #         log["timestamp"] = log["timestamp"].isoformat()
                    status = dict(status, **db_job)
    except Exception as e:  # pylint: disable=broad-exception-caught
        status["error"] = f"{e}"

    return status


def get_actions():
    """Return the action functions provided by this extension."""
    actions = {
        "aiextract_delete": aiextract_delete,
        "aiextract_update": aiextract_update,
        "aiextract_update_status": aiextract_update_status,
        "aiextract_hook": aiextract_hook,
    }
    return actions
