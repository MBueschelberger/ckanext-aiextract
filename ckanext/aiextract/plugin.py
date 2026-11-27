"""CKAN plugin entry point for ckanext-aiextract.

Registers configuration, actions, auth functions, template helpers and blueprints,
and submits matching resources to the aiextract update action on create/update.
"""

import logging
import os
from typing import Any

import ckan.plugins as p
from ckan import model
from ckan.common import CKANConfig
from ckan.config.declaration import Declaration, Key

from ckanext.aiextract import db, helpers, views
from ckanext.aiextract.logic import action, auth

if p.toolkit.check_ckan_version("2.10"):
    from ckan.types import Context  # pylint: disable=ungrouped-imports
else:

    class Context(dict):
        """Fallback Context type for CKAN versions older than 2.10."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)


log = logging.getLogger(__name__)

DEFAULT_FORMATS = [
    "pdf",
]


class AiextractPlugin(p.SingletonPlugin):
    """CKAN plugin that triggers AI extraction for resources and exposes its actions."""

    p.implements(p.IConfigurer)
    p.implements(p.IConfigDeclaration)
    p.implements(p.IActions)
    p.implements(p.IAuthFunctions)
    p.implements(p.IResourceUrlChange)
    p.implements(p.IResourceController, inherit=True)
    p.implements(p.ITemplateHelpers)
    p.implements(p.IBlueprint)

    def update_config(self, config: CKANConfig):
        """Check required config, register templates and assets and initialise the db."""
        required_keys = ""  # include ckan.ini entities to test for here space separated
        for key in required_keys.split():
            if config.get(key) is None:
                # check if in envars
                env_str = "CKANINI__" + key.replace(".", "__").upper()
                if not os.environ.get(env_str, None):
                    raise RuntimeError(
                        f"Required configuration option {key} not found in ckan.ini or ENVARS."
                    )
        self.config = config  # pylint: disable=attribute-defined-outside-init
        p.toolkit.add_template_directory(config, "templates")
        p.toolkit.add_resource("assets", "aiextract")
        db.init()

    # IConfigDeclaration

    def declare_config_options(self, declaration: Declaration, key: Key):
        """Declare the ckanext.aiextract.* config options and their defaults."""

        declaration.annotate("AIxtract")
        group = key.ckanext.aiextract
        declaration.declare_bool(group.ssl_verify, True)
        declaration.declare(group.db_url, p.toolkit.config.get("sqlalchemy.url"))
        declaration.declare(group.ckan_token, "")
        declaration.declare(group.formats, "pdf")
        declaration.declare(group.extract_url, "")
        declaration.declare(group.job_timeout, 1800)

    # IResourceUrlChange

    def notify(self, resource: model.Resource):
        """Submit a resource to aiextract when its URL changes."""
        context: Context = {"ignore_auth": True}
        resource_dict = p.toolkit.get_action("resource_show")(
            context,
            {
                "id": resource.id,
            },
        )
        self._sumbit_to_extract(resource_dict)

    # IResourceController

    def after_resource_create(  # pylint: disable=unused-argument
        self, context: Context, resource_dict: dict[str, Any]
    ):
        """Submit a newly created resource to aiextract."""
        self._sumbit_to_extract(resource_dict)

    def after_update(  # pylint: disable=unused-argument
        self, context: Context, resource_dict: dict[str, Any]
    ):
        """Submit an updated resource to aiextract."""
        self._sumbit_to_extract(resource_dict)

    def _sumbit_to_extract(self, resource_dict: dict[str, Any]):
        context = {"model": model, "ignore_auth": True, "defer_commit": True}
        fmt = resource_dict.get("format", None)
        submit = fmt and fmt.lower() in DEFAULT_FORMATS
        if not submit:
            return
        try:
            log.debug("Submitting resource %s to aiextract_update", resource_dict["id"])
            p.toolkit.get_action("aiextract_update")(
                context,
                {
                    "pkg_id": resource_dict["package_id"],
                    "resource_ids": [
                        resource_dict["id"],
                    ],
                },
            )

        except p.toolkit.ValidationError as e:
            # If Extraction API is offline want to catch error instead
            # of raising otherwise resource save will fail with 500
            log.critical(e)
        return

    # IActions

    def get_actions(self):
        """Return the aiextract action functions."""
        actions = action.get_actions()
        return actions

    # IAuthFunctions

    def get_auth_functions(self):
        """Return the aiextract auth functions."""
        return auth.get_auth_functions()

    # ITemplateHelpers

    def get_helpers(self):
        """Return the aiextract template helpers."""
        return helpers.get_helpers()

    # IBlueprint

    def get_blueprint(self):
        """Return the aiextract Flask blueprint."""
        return views.get_blueprint()
