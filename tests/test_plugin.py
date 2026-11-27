"""Unit tests for the submission hook of ckanext.aiextract.plugin (F1)."""

# pytest idioms: fixtures as arguments (also for side effects only), self-describing names
# pylint: disable=redefined-outer-name,unused-argument,missing-function-docstring

from types import SimpleNamespace

import pytest

from ckanext.aiextract import plugin
from ckanext.aiextract.logic import action

PDF_RESOURCE = {"id": "res-1", "package_id": "pkg-1", "format": "PDF"}


@pytest.fixture
def aiextract_plugin():
    """Return the plugin instance (not loaded into CKAN)."""
    return plugin.AiextractPlugin()


@pytest.fixture
def submitted(actions):
    """Record calls of the ``aiextract_update`` action."""
    actions.register("aiextract_update", True)
    actions.register("resource_show", lambda context, data_dict: dict(PDF_RESOURCE))
    return actions


def update_calls(actions):
    """Return (context, data_dict) of all ``aiextract_update`` calls."""
    return [(context, data) for name, context, data in actions.calls if name == "aiextract_update"]


def test_submit_context_has_no_defer_commit(aiextract_plugin, submitted):
    aiextract_plugin._sumbit_to_extract(dict(PDF_RESOURCE))  # pylint: disable=protected-access

    [(context, data_dict)] = update_calls(submitted)
    assert "defer_commit" not in context
    assert context["ignore_auth"] is True
    assert not context.get(action.WITHIN_COMMIT)
    assert data_dict == {"pkg_id": "pkg-1", "resource_ids": ["res-1"]}


def test_after_resource_create_submits_without_defer_commit(aiextract_plugin, submitted):
    aiextract_plugin.after_resource_create({}, dict(PDF_RESOURCE))

    [(context, _)] = update_calls(submitted)
    assert "defer_commit" not in context
    assert not context.get(action.WITHIN_COMMIT)


def test_notify_submits_within_commit(aiextract_plugin, submitted):
    # CKAN calls IResourceUrlChange.notify from model.Session's before_commit hook.
    aiextract_plugin.notify(SimpleNamespace(id="res-1"))

    [(context, data_dict)] = update_calls(submitted)
    assert "defer_commit" not in context
    assert context[action.WITHIN_COMMIT] is True
    assert data_dict["resource_ids"] == ["res-1"]


def test_non_pdf_resource_is_not_submitted(aiextract_plugin, submitted):
    resource = dict(PDF_RESOURCE, format="CSV")
    aiextract_plugin._sumbit_to_extract(resource)  # pylint: disable=protected-access

    assert not update_calls(submitted)
