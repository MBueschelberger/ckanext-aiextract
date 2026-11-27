"""Authorization functions for the aiextract CKAN extension."""

import ckan.plugins as p
from ckan.logic.auth.get import task_status_show


def aiextract_auth(context, data_dict, privilege="package_update"):
    """Check the given CKAN privilege for the dataset or resource referenced in data_dict.

    ``pkg_id`` (forcing ``package_update``) or the first entry of
    ``resource_ids`` is copied to ``data_dict["id"]`` before the check.
    """
    if "pkg_id" in data_dict.keys():
        data_dict["id"] = data_dict["pkg_id"]
        privilege = "package_update"
    if "id" not in data_dict and "resource_ids" in data_dict:
        data_dict["id"] = data_dict.get("resource_ids", [None])[0]
    user = context.get("user")
    authorized = p.toolkit.check_access(privilege, context, data_dict)
    if not authorized:
        return {
            "success": False,
            "msg": p.toolkit._(
                f"User {str(user)} not authorized to update package {data_dict['id']}"
            ),
        }
    return {"success": True}


def aiextract_delete(context, data_dict):
    """Authorize deleting aiextract data (requires ``package_update``)."""
    return aiextract_auth(context, data_dict)


@p.toolkit.auth_allow_anonymous_access
def aiextract_search(context, data_dict):
    """Authorize searching aiextract data (requires ``resource_show``)."""
    return aiextract_auth(context, data_dict, "resource_show")


def aiextract_update(context, data_dict):
    """Authorize an aiextract update (``package_update`` or ``resource_update``)."""
    if "resource" in data_dict and data_dict["resource"].get("package_id"):
        data_dict["id"] = data_dict["resource"].get("package_id")
        privilege = "package_update"
    else:
        privilege = "resource_update"
    return aiextract_auth(context, data_dict, privilege=privilege)


def aiextract_update_status(context, data_dict):
    """Authorize reading aiextract status (delegates to ``task_status_show``)."""
    return task_status_show(context, data_dict)


def get_auth_functions():
    """Return the auth functions provided by this extension."""
    return {
        "aiextract_delete": aiextract_delete,
        "aiextract_update": aiextract_update,
        "aiextract_update_status": aiextract_update_status,
        "aiextract_search": aiextract_search,
    }
