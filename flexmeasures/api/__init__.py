"""
FlexMeasures API routes and implementations.
"""

from flask import Flask, Blueprint, request, current_app
import html
from flask_security.utils import verify_password
from flask_json import as_json
from flask_login import current_user
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select

from flexmeasures.data import db
from flexmeasures import __version__ as flexmeasures_version
from flexmeasures.api.common.utils.api_utils import catch_timed_belief_replacements
from flexmeasures.data.models.user import User
from flexmeasures.api.common.utils.args_parsing import (
    validation_error_handler,
)
from flexmeasures.api.common.responses import invalid_sender
from flexmeasures.data.schemas.utils import FMValidationError

# The api blueprint. It is registered with the Flask app (see app.py)
flexmeasures_api = Blueprint("flexmeasures_api", __name__)


@flexmeasures_api.route("/requestAuthToken", methods=["POST"])
@as_json
def request_auth_token():
    """API endpoint to get a fresh authentication access token. Be aware that this fresh token has a limited lifetime
    (which depends on the current system setting SECURITY_TOKEN_MAX_AGE).

    Pass the `email` parameter to identify the user.
    Pass the `password` parameter to authenticate the user (if not already authenticated in current session)

    .. :quickref: Public; Obtain an authentication token
    """

    """
    The login of flask security returns the auth token, as well, but we'd like to
     * skip authentication if the user is authenticated already
     * be exempt from csrf protection (this is a JSON-only endpoint)
     * use a more fitting name inside the api namespace
     * return the information in a nicer structure
    """
    try:
        if not request.is_json:
            return {"errors": ["Content-type of request must be application/json"]}, 400
        if "email" not in request.json:
            return {"errors": ["Please provide the 'email' parameter."]}, 400

        email = request.json["email"]
        if current_user.is_authenticated and current_user.email == email:
            user = current_user
        else:
            user = db.session.execute(
                select(User).filter_by(email=email)
            ).scalar_one_or_none()
            if not user:
                return (
                    {
                        "errors": [
                            "User with email '%s' does not exist" % html.escape(email)
                        ]
                    },
                    404,
                )

            if "password" not in request.json:
                return {"errors": ["Please provide the 'password' parameter."]}, 400
            if not verify_password(request.json["password"], user.password):
                return {"errors": ["User password does not match."]}, 401
        token = user.get_auth_token()
        return {"auth_token": token, "user_id": user.id}
    except Exception as e:
        current_app.logger.error(f"Exception in /requestAuthToken endpoint: {e}")
        return {"errors": ["An internal error has occurred."]}, 500


@flexmeasures_api.route("/", methods=["GET"])
@as_json
def get_versions() -> dict:
    """Public endpoint to list API versions.

    .. :quickref: Public; List available API versions

    """
    response = {
        "message": "For these API versions a public endpoint is available, listing its service. For example: "
        "/api/v3_0. An authentication token can be requested at: "
        "/api/requestAuthToken",
        "versions": ["v3_0"],
        "flexmeasures_version": flexmeasures_version,
    }
    return response


def register_at(app: Flask):
    """This can be used to register this blueprint together with other api-related things"""

    # handle API specific errors
    app.register_error_handler(FMValidationError, validation_error_handler)
    app.register_error_handler(IntegrityError, catch_timed_belief_replacements)
    app.unauthorized_handler_api = invalid_sender

    app.register_blueprint(
        flexmeasures_api, url_prefix="/api"
    )  # now registering the blueprint will affect all endpoints

    # Load API endpoints for internal operations
    from flexmeasures.api.common import register_at as ops_register_at

    ops_register_at(app)

    # Load API endpoints for play mode
    if app.config.get("FLEXMEASURES_MODE", "") == "play":
        from flexmeasures.api.play import register_at as play_register_at

        play_register_at(app)

    # Load all versions of the API functionality
    from flexmeasures.api.v3_0 import register_at as v3_0_register_at
    from flexmeasures.api.dev import register_at as dev_register_at
    from flexmeasures.api.sunset import register_at as sunset_register_at

    v3_0_register_at(app)
    dev_register_at(app)
    sunset_register_at(app)
