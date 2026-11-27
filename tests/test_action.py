"""Unit tests for ckanext.aiextract.logic.action (task_status handling, F2/F2b/F4)."""

# pytest idioms: fixtures as arguments (also for side effects only), self-describing names
# pylint: disable=redefined-outer-name,unused-argument,missing-function-docstring

import datetime
import inspect
import json
import os
import time
from unittest import mock

import pytest
from ckan.plugins import toolkit

from ckanext.aiextract.logic import action

STALE = datetime.timedelta(days=3)
FRESH = datetime.timedelta(seconds=5)


def not_found(context, data_dict):
    """Fake ``task_status_show`` for resources without a task."""
    raise toolkit.ObjectNotFound()


# --- F4: ready_for_task gets the resource id --------------------------------------


def test_aiextract_update_checks_each_resource_id(actions, monkeypatch, make_package):
    actions.register("package_show", make_package("res-1", "res-2"))
    actions.register("aiextract_update_status", {"tasks": {}})
    ready = mock.Mock(return_value=True)
    enqueue = mock.Mock(return_value=True)
    monkeypatch.setattr(action, "ready_for_task", ready)
    monkeypatch.setattr(action, "enqueue_update", enqueue)

    result = action.aiextract_update(
        {"ignore_auth": True}, {"pkg_id": "pkg-1", "resource_ids": ["res-1", "res-2"]}
    )

    assert result is True
    assert [c.args[1] for c in ready.call_args_list] == ["res-1", "res-2"]
    assert [c.args[3] for c in enqueue.call_args_list] == ["res-1", "res-2"]
    # the status passed to ready_for_task is queried per resource
    status_calls = [d for n, _, d in actions.calls if n == "aiextract_update_status"]
    assert [[pdf["id"] for pdf in d["pdfs"]] for d in status_calls] == [["res-1"], ["res-2"]]


def test_aiextract_update_skips_only_busy_resource(actions, monkeypatch, make_package):
    actions.register("package_show", make_package("res-1", "res-2"))
    actions.register("aiextract_update_status", {"tasks": {}})
    monkeypatch.setattr(action, "ready_for_task", lambda ctx, res_id, status: res_id == "res-2")
    enqueue = mock.Mock(return_value=True)
    monkeypatch.setattr(action, "enqueue_update", enqueue)

    action.aiextract_update({}, {"pkg_id": "pkg-1", "resource_ids": ["res-1", "res-2"]})

    assert [c.args[3] for c in enqueue.call_args_list] == ["res-2"]


def test_aiextract_delete_checks_each_resource_id(actions, monkeypatch, make_package):
    actions.register("package_show", make_package("res-1", "res-2"))
    actions.register("aiextract_update_status", {"tasks": {}})
    actions.register("resource_delete", None)
    ready = mock.Mock(return_value=True)
    monkeypatch.setattr(action, "ready_for_task", ready)
    search = mock.Mock(return_value=[{"id": "derived-1"}])
    monkeypatch.setattr(action.backend, "resource_search", search)

    result = action.aiextract_delete({}, {"pkg_id": "pkg-1", "resource_ids": ["res-1", "res-2"]})

    assert result is True
    assert [c.args[1] for c in ready.call_args_list] == ["res-1", "res-2"]
    assert "pkg-1" not in [c.args[1] for c in ready.call_args_list]
    deleted = [d for n, _, d in actions.calls if n == "resource_delete"]
    assert deleted == [{"id": "derived-1"}, {"id": "derived-1"}]


def test_aiextract_delete_skips_resource_with_running_task(actions, monkeypatch, make_package):
    actions.register("package_show", make_package("res-1"))
    actions.register("aiextract_update_status", {"tasks": {}})
    monkeypatch.setattr(action, "ready_for_task", mock.Mock(return_value=False))
    search = mock.Mock(return_value=[{"id": "derived-1"}])
    monkeypatch.setattr(action.backend, "resource_search", search)

    assert action.aiextract_delete({}, {"pkg_id": "pkg-1", "resource_ids": ["res-1"]}) is False
    search.assert_not_called()
    assert "resource_delete" not in actions.names()


# --- F2b: ready_for_task is read-only on task_status ------------------------------


@pytest.mark.parametrize(
    ("state", "age"),
    [("running", STALE), ("pending", STALE), ("complete", FRESH), ("complete", STALE)],
)
def test_ready_for_task_does_not_delete_old_tasks(actions, enqueued, make_task, state, age):
    actions.register("task_status_show", make_task("res-1", state, age))

    assert action.ready_for_task({}, "res-1", {}) is True
    assert actions.names() == ["task_status_show"]  # no task_status_delete/_update


def test_ready_for_task_false_for_fresh_unfinished_task(actions, enqueued, make_task):
    actions.register("task_status_show", make_task("res-1", "running", FRESH))

    assert action.ready_for_task({}, "res-1", {}) is False


@pytest.fixture
def local_time_utc_plus_14():
    """Run the test with a local time zone far away from UTC."""
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "XYZ-14"  # POSIX notation for UTC+14
    time.tzset()
    try:
        yield
    finally:
        if old_tz is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


@pytest.mark.usefixtures("local_time_utc_plus_14")
def test_ready_for_task_treats_naive_timestamp_as_utc(actions, enqueued, make_task):
    # A task updated 5 s ago must not look stale on servers with a non-UTC local time.
    actions.register("task_status_show", make_task("res-1", "pending", FRESH))

    assert action.ready_for_task({}, "res-1", {}) is False


def test_ready_for_task_true_without_task(actions, enqueued):
    actions.register("task_status_show", not_found)

    assert action.ready_for_task({}, "res-1", {}) is True


def test_ready_for_task_removes_old_job_record(actions, enqueued, make_task):
    actions.register("task_status_show", make_task("res-1", "complete", STALE))
    action.db.get_job.return_value = {"job_id": "old-job"}

    assert action.ready_for_task({}, "res-1", {"job_id": "old-job"}) is True
    action.db.delete_job.assert_called_once_with("old-job")  # pylint: disable=no-member


# --- F2: a single session (model.Session) writes task_status ----------------------


def test_enqueue_update_overwrites_stale_task_in_main_session(
    actions, enqueued, fake_model, site_config, make_task
):
    actions.register("task_status_show", make_task("res-1", "running", STALE))
    existing = fake_model.TaskStatus(id="task-res-1", state="running", value='{"job_id": "old"}')
    query = fake_model.Session.query.return_value.filter_by
    query.return_value.first.return_value = existing

    assert action.enqueue_update({}, "my-dataset", "pkg-1", "res-1", "changed") is True

    query.assert_called_once_with(entity_id="res-1", task_type="aiextract", key="aiextract")
    fake_model.Session.add.assert_called_once_with(existing)
    fake_model.Session.commit.assert_called_once_with()
    assert existing.id == "task-res-1"
    assert existing.state == "pending"
    assert json.loads(existing.value) == {"job_id": enqueued[0].id}
    assert existing.entity_type == "resource"
    fake_model.meta.create_local_session.assert_not_called()
    assert "task_status_delete" not in actions.names()
    assert "task_status_update" not in actions.names()


def test_enqueue_update_creates_task_if_none_exists(actions, enqueued, fake_model, site_config):
    actions.register("task_status_show", not_found)

    assert action.enqueue_update({}, "my-dataset", "pkg-1", "res-1", "changed") is True

    (created,), _ = fake_model.Session.add.call_args
    assert isinstance(created, fake_model.TaskStatus)
    assert created.entity_id == "res-1"
    assert created.task_type == "aiextract"
    assert created.key == "aiextract"
    assert created.state == "pending"
    assert json.loads(created.value) == {"job_id": "job-1"}
    fake_model.Session.commit.assert_called_once_with()


def test_enqueue_update_within_commit_does_not_commit(actions, enqueued, fake_model, site_config):
    actions.register("task_status_show", not_found)

    context = {action.WITHIN_COMMIT: True}
    assert action.enqueue_update(context, "my-dataset", "pkg-1", "res-1", "changed") is True

    fake_model.Session.add.assert_called_once()
    fake_model.Session.commit.assert_not_called()
    fake_model.meta.create_local_session.assert_not_called()


def test_enqueue_update_skips_fresh_pending_task(
    actions, enqueued, fake_model, site_config, make_task
):
    actions.register("task_status_show", make_task("res-1", "pending", FRESH))

    assert action.enqueue_update({}, "my-dataset", "pkg-1", "res-1", "changed") is False
    assert not enqueued
    fake_model.Session.add.assert_not_called()


def test_action_module_has_no_second_session():
    source = inspect.getsource(action)
    assert "create_local_session" not in source
    assert "defer_commit" not in source


@pytest.mark.usefixtures("site_config")
def test_aiextract_update_stale_task_single_write(
    actions, enqueued, fake_model, make_package, make_task
):
    """Full update flow for a stale task: no delete, one write, one commit."""
    actions.register("package_show", make_package("res-1"))
    actions.register("aiextract_update_status", {"tasks": {}})
    actions.register("task_status_show", make_task("res-1", "running", STALE))
    existing = fake_model.TaskStatus(id="task-res-1", state="running")
    fake_model.Session.query.return_value.filter_by.return_value.first.return_value = existing

    assert action.aiextract_update({}, {"pkg_id": "pkg-1", "resource_ids": ["res-1"]}) is True

    assert len(enqueued) == 1
    assert "task_status_delete" not in actions.names()
    assert "task_status_update" not in actions.names()
    fake_model.Session.add.assert_called_once_with(existing)
    fake_model.Session.commit.assert_called_once_with()
    assert existing.state == "pending"


# --- 4.2: callback URL -----------------------------------------------------------


def test_callback_url_uses_callback_base_url(
    actions, enqueued, fake_model, site_config, monkeypatch
):
    monkeypatch.setitem(toolkit.config, "ckanext.aiextract.callback_base_url", "http://ckan:5000/")
    actions.register("task_status_show", not_found)

    action.enqueue_update({}, "my-dataset", "pkg-1", "res-1", "changed")

    expected = "http://ckan:5000/api/3/action/aiextract_hook"
    assert enqueued[0].args[3] == expected
    pending_job_kwargs = action.db.add_pending_job.call_args.kwargs  # pylint: disable=no-member
    assert pending_job_kwargs["result_url"] == expected


def test_callback_url_defaults_to_site_url(actions, enqueued, fake_model, site_config):
    actions.register("task_status_show", not_found)

    action.enqueue_update({}, "my-dataset", "pkg-1", "res-1", "changed")

    assert enqueued[0].args[3] == "https://data.example.org/api/3/action/aiextract_hook"
