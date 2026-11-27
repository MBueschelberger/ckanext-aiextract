"""Shared fixtures for the ckanext-aiextract unit tests.

The tests run without a CKAN config, database, Solr or Redis: CKAN actions,
``enqueue_job`` and the database layers are replaced by fakes. CKAN's own pytest
plugin is disabled in ``pyproject.toml`` (``-p no:ckan``).
"""

from types import SimpleNamespace
from unittest import mock

import pytest
from ckan.plugins import toolkit

from ckanext.aiextract.logic import action
from ckanext.aiextract.utils import utcnow

SITE_URL = "https://data.example.org"
DATASET_ID = "pkg-1"
DATASET_NAME = "my-dataset"


class FakeActions:
    """Registry of fake CKAN actions that records every call."""

    def __init__(self):
        self.handlers = {}
        self.calls = []

    def register(self, name, handler):
        """Register ``handler(context, data_dict)`` or a constant return value."""
        self.handlers[name] = handler if callable(handler) else (lambda c, d, v=handler: v)

    def get_action(self, name):
        """Replacement for ``toolkit.get_action``; unknown actions fail the test."""

        def call(context=None, data_dict=None):
            context = {} if context is None else context
            self.calls.append((name, context, data_dict))
            if name not in self.handlers:
                raise AssertionError(f"unexpected call of action {name!r}")
            return self.handlers[name](context, data_dict)

        return call

    def names(self):
        """Return the names of all called actions in call order."""
        return [name for name, _, _ in self.calls]


class FakeTaskStatus:
    """Stand-in for ``ckan.model.TaskStatus``."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture
def actions(monkeypatch):
    """Fake CKAN action registry; ``check_access`` always succeeds."""
    fake = FakeActions()
    monkeypatch.setattr(toolkit, "get_action", fake.get_action)
    monkeypatch.setattr(toolkit, "check_access", lambda *args, **kwargs: True)
    return fake


@pytest.fixture
def site_config(monkeypatch):
    """Set ``ckan.site_url`` and make sure no callback base URL is configured."""
    monkeypatch.setitem(toolkit.config, "ckan.site_url", SITE_URL)
    monkeypatch.delitem(toolkit.config, "ckanext.aiextract.callback_base_url", raising=False)
    return toolkit.config


@pytest.fixture
def fake_model(monkeypatch):
    """Replace ``ckan.model`` in the action module by a fake with a mocked Session."""
    session = mock.MagicMock(name="model.Session")
    session.query.return_value.filter_by.return_value.first.return_value = None
    fake = SimpleNamespace(
        Session=session,
        TaskStatus=FakeTaskStatus,
        meta=SimpleNamespace(
            create_local_session=mock.Mock(side_effect=AssertionError("second session used"))
        ),
    )
    monkeypatch.setattr(action, "model", fake)
    return fake


@pytest.fixture
def enqueued(monkeypatch):
    """Fake ``toolkit.enqueue_job`` and the extension's job database."""
    jobs = []

    def enqueue_job(fn, args=None, **kwargs):
        job = SimpleNamespace(id=f"job-{len(jobs) + 1}", fn=fn, args=args, kwargs=kwargs)
        jobs.append(job)
        return job

    monkeypatch.setattr(toolkit, "enqueue_job", enqueue_job)
    monkeypatch.setattr(toolkit, "url_for", lambda *args, **kwargs: "/api/3/action/aiextract_hook")
    monkeypatch.setattr(action.db, "add_pending_job", mock.Mock())
    monkeypatch.setattr(action.db, "get_job", mock.Mock(return_value=None))
    monkeypatch.setattr(action.db, "delete_job", mock.Mock())
    return jobs


@pytest.fixture
def make_task():
    """Factory for task_status dicts as returned by ``task_status_show``.

    ``last_updated`` is naive UTC, like CKAN stores it.
    """

    def factory(res_id, state, age, job_id="old-job"):
        last_updated = utcnow() - age
        return {
            "id": f"task-{res_id}",
            "entity_id": res_id,
            "entity_type": "resource",
            "task_type": "aiextract",
            "key": "aiextract",
            "value": f'{{"job_id": "{job_id}"}}',
            "state": state,
            "error": "{}",
            "last_updated": last_updated.isoformat(),
        }

    return factory


@pytest.fixture
def make_package():
    """Factory for a minimal package dict with one PDF resource per given id."""

    def factory(*res_ids):
        return {
            "id": DATASET_ID,
            "name": DATASET_NAME,
            "resources": [
                {
                    "id": res_id,
                    "package_id": DATASET_ID,
                    "format": "PDF",
                    "url": f"{SITE_URL}/dataset/{DATASET_ID}/resource/{res_id}/download/x.pdf",
                }
                for res_id in res_ids
            ],
        }

    return factory
