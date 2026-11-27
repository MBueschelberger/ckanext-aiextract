"""Flask blueprint and views for the aiextract CKAN extension."""

import ckan.lib.helpers as core_helpers
from ckan import logic
from ckan.common import _, current_user, request
from ckan.lib import base
from ckan.plugins import toolkit
from flask import Blueprint
from flask.views import MethodView

from ckanext.aiextract.helpers import aiextract_service_available, aiextract_show_tools

if toolkit.check_ckan_version("2.10"):
    from ckan.types import Context  # pylint: disable=ungrouped-imports
else:

    class Context(dict):
        """Fallback for ``ckan.types.Context`` on CKAN versions before 2.10."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)


log = __import__("logging").getLogger(__name__)


blueprint = Blueprint("aiextract", __name__)


class AiextractView(MethodView):
    """Dataset page for triggering and showing aiextract processing of resources."""

    def post(self, id: str):  # pylint: disable=redefined-builtin
        """Queue update or deletion of the selected resources, then redirect to the GET view."""
        # try:
        con_dict: Context = {"user": current_user.name, "auth_user_obj": current_user}
        context = logic.fresh_context(con_dict)
        pkg_dict = toolkit.get_action("package_show")(context, {"id": id})
        if "extract/update" in request.form:
            to_upload = request.form.getlist("resid")
            log.debug("resource ids to upload: %s", to_upload)
            if to_upload:
                toolkit.get_action("aiextract_update")(
                    context, {"pkg_id": pkg_dict["id"], "resource_ids": to_upload}
                )
        elif "delete" in request.form:
            to_delete = request.form.getlist("resid")
            toolkit.get_action("aiextract_delete")(
                context, {"pkg_id": pkg_dict["id"], "resource_ids": to_delete}
            )
        # except toolkit.ObjectNotFound:
        #     base.abort(404, "Dataset not found")
        # except toolkit.NotAuthorized:
        #     base.abort(403, _("Not authorized to see this page"))

        return core_helpers.redirect_to("aiextract.aiextract", id=pkg_dict["id"])

    def get(self, id: str):  # pylint: disable=redefined-builtin
        """Render the aiextract status page for the dataset's supported resources."""
        con_dict: Context = {"user": current_user.name, "auth_user_obj": current_user}
        context = logic.fresh_context(con_dict)
        pkg_dict = toolkit.get_action("package_show")(context, {"id": id})
        pdf_ressources = [res for res in pkg_dict["resources"] if aiextract_show_tools(res)]

        try:
            pkg_dict = toolkit.get_action("package_show")(context, {"id": id})
            status = toolkit.get_action("aiextract_update_status")(
                context, {"pdfs": pdf_ressources}
            )
        except toolkit.ObjectNotFound:
            base.abort(404, "Dataset not found")
        except toolkit.NotAuthorized:
            base.abort(403, _("Not authorized to see this page"))

        return base.render(
            "aiextract/status.html",
            extra_vars={
                "pkg_dict": pkg_dict,
                "resources": pdf_ressources,
                "status": status,
                "service_status": aiextract_service_available(),
                "refresh_rate": 10,
            },
        )


class StatusView(MethodView):
    """Return the aiextract processing status of a dataset as a dict."""

    def get(self, id: str):  # pylint: disable=redefined-builtin
        """Return the dataset and its aiextract status, with display hints added to the logs."""
        pkg_dict = {}
        con_dict: Context = {"user": current_user.name, "auth_user_obj": current_user}
        context = logic.fresh_context(con_dict)
        try:
            pkg_dict = toolkit.get_action("package_show")(context, {"id": id})
            pdf_ressources = [res for res in pkg_dict["resources"] if aiextract_show_tools(res)]
            # log.debug(pdf_ressources)
            if pdf_ressources:
                status = toolkit.get_action("aiextract_update_status")(
                    context, {"pdfs": pdf_ressources}
                )
            else:
                status = {}
        except toolkit.ObjectNotFound:
            base.abort(404, "Dataset not found")
        except toolkit.NotAuthorized:
            base.abort(403, _("Not authorized to see this page"))

        if "logs" in status.keys():
            for index, item in enumerate(status["logs"]):
                # status["logs"][index]["timestamp"] = (
                #     core_helpers.time_ago_from_timestamp(item["timestamp"])
                # )
                if item["level"] == "DEBUG":
                    status["logs"][index]["alertlevel"] = "info"
                    status["logs"][index]["icon"] = "bug-slash"
                    status["logs"][index]["class"] = "success"
                elif item["level"] == "INFO":
                    status["logs"][index]["alertlevel"] = "info"
                    status["logs"][index]["icon"] = "check"
                    status["logs"][index]["class"] = "success"
                else:
                    status["logs"][index]["alertlevel"] = "error"
                    status["logs"][index]["icon"] = "exclamation"
                    status["logs"][index]["class"] = "failure"
        return {"pkg_dict": pkg_dict, "status": status}


blueprint.add_url_rule("/dataset/<id>/aiextract", view_func=AiextractView.as_view("aiextract"))
blueprint.add_url_rule(
    "/dataset/<id>/aiextract/status",
    view_func=StatusView.as_view("status"),
)


def get_blueprint():
    """Return the aiextract Flask blueprint."""
    return blueprint
