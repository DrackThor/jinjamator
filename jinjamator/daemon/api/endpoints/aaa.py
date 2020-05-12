import logging

from flask import request, flash, url_for, g, Response
from flask import Flask, url_for, session, redirect

from flask_restx import Resource, abort
from jinjamator.daemon.aaa import aaa_providers
from jinjamator.daemon.api.restx import api
from jinjamator.daemon.api.serializers import (
    aaa_login_post,
    aaa_create_user,
    aaa_create_role,
    aaa_set_user_roles,
    environments,
)
from jinjamator.daemon.aaa.models import User, JinjamatorRole
from jinjamator.daemon.database import db

from flask import current_app as app
import glob
import os
import xxhash
from pprint import pformat
from datetime import datetime
from calendar import timegm

log = logging.getLogger()

ns = api.namespace(
    "aaa",
    description="Operations related to jinjamator Authentication Authorization and Accounting",
)

current_provider = None


@ns.route("/login/<provider>")
class Login(Resource):
    @api.response(400, "Unknown Login Provider <provider>")
    @api.response(200, "Success")
    @api.response(302, "Redirect to IDP")
    def get(self, provider="local"):
        """
        Login User via GET request.
        OIDC providers will redirect to the authentication portal of the IDp, local authentication will directly return an access_token
        Won't work from swaggerui with okta provider due okta not sending cors headers from authorize endpoint.
        For testing OIDC via okta copy the url generated by swagger and c&p it to a new browser tab.
        """
        if provider in aaa_providers:
            response = aaa_providers[provider].login(request)
            # try:
            #     response.headers.add("Access-Control-Allow-Origin", "*")
            # except AttributeError:
            #     log.debug('Cannot add Access-Control-Allow-Origin header')
            #     pass
            return response
        abort(400, f"Unknown Login Provider {provider}")

    @api.expect(aaa_login_post)
    def post(self, provider="local"):
        """
        Login User via POST request. 
        OIDC providers will redirect to the authentication portal of the IDp, local authentication will directly return an access_token
        """
        if provider in aaa_providers:
            return aaa_providers[provider].login(request)
        abort(400, f"Unknown Login Provider {provider}")


@ns.route("/logout")
@api.response(400, "Not logged in")
@api.response(200, "Success")
class Logout(Resource):
    def get(self):
        """
        Logout User and terminate all session information.
        """
        if current_provider:

            return {"message": "not implemented"}
        else:
            abort(400, "Not logged in")


@ns.route("/auth")
@api.response(401, "Upstream token expired, please reauthenticate")
@api.response(301, "Redirect to jinjamator web with access_token as GET parameter")
@api.response(400, "Cannot find valid login provider")
class Auth(Resource):
    def get(self):
        """
        Extract token from OIDC authentication flow and redirect to jinjamator web with access_token as GET parameter.
        This REST Endpoint should never be called directly.
        """
        for aaa_provider in aaa_providers:
            log.debug(f"trying to use {aaa_provider}")
            if aaa_providers[aaa_provider].authorize(request):
                current_provider = aaa_providers[aaa_provider]
                token = current_provider.get_token()
                if token:
                    url = url_for("webui.index", access_token=token)
                    proto = request.headers.get("X-Forwarded-Proto", "http")
                    url = url.replace("http", proto)
                    redir = redirect(url)
                    return redir
                else:
                    abort(401, "Upstream token expired, please reauthenticate")
        abort(400, "Cannot find valid login provider")


@ns.route("/verify")
class VerifyToken(Resource):
    @api.doc(
        params={
            "Authorization": {"in": "header", "description": "An authorization token"}
        }
    )
    def get(self):
        auth_header = request.headers.get("Authorization")
        if auth_header:
            try:
                token_type, auth_token = auth_header.split(" ")
            except:
                abort(400, "Invalid Authorization Header Format")
            if token_type == "Bearer":
                token_data = User.verify_auth_token(auth_token)
                if token_data:
                    now = timegm(datetime.utcnow().utctimetuple())
                    log.info((token_data["exp"] - now))
                    if (token_data["exp"] - now) < 300:
                        log.info("renewing token as lifetime less than 300s")
                        token = (
                            User.query.filter_by(id=token_data["id"])
                            .first()
                            .generate_auth_token()
                            .access_token
                        )
                        return (
                            {
                                "message": f'login ok user id {token_data["id"]}, you got a new token',
                                "status": "logged_in_new_token_issued",
                                "user_id": token_data["id"],
                                "token_ttl": token_data["exp"] - now,
                            },
                            200,
                            {"access_token": f"Bearer {token}"},
                        )

                    return {
                        "message": f'login ok user id {token_data["id"]}',
                        "status": "logged_in",
                        "user_id": token_data["id"],
                        "token_ttl": token_data["exp"] - now,
                        "auto_renew_in": token_data["exp"] - now - 300,
                    }

                else:
                    abort(400, "Token invalid, please reauthenticate")
            else:
                abort(400, "Invalid Authorization Header Token Type")
        else:
            abort(401, "Authorization required, no Authorization Header found")


@ns.route("/users")
@api.response(400, "Parameters missing, or not properly encoded")
@api.response(200, "Success")
class Users(Resource):
    def get(self):
        """
        List all registred users.
        """
        retval = []
        for user in User.query.all():
            retval.append(user.to_dict())
        return retval

    @api.expect(aaa_create_user)
    def post(self):
        """
        Create a new Jinjamator User.
        """
        try:
            new_user = User(
                username=request.json["username"],
                name=request.json["name"],
                password_hash=User.hash_password(request.json["password"]),
                aaa_provider=request.json.get("aaa_provider", "local"),
            )
        except IndexError:
            abort(400, "Parameters missing, or not properly encoded")
        db.session.add(new_user)
        try:
            db.session.commit()
            db.session.refresh(new_user)
            return new_user.to_dict()
        except:
            abort(400, "User exists")


@ns.route("/users/<user_id>")
class UserDetail(Resource):
    @api.doc(params={"user_id": "The User ID of the user which should be returned"})
    def get(self, user_id):
        """
        Get details about a user.
        """
        user = User.query.filter_by(id=user_id).first()
        if user:
            return user.to_dict()
        else:
            abort(404, "User ID not found")


@ns.route("/users/<user_id>/roles")
@api.doc(params={"user_id": "The User ID of the user which roles should be returned"})
class UserRolesDetail(Resource):
    def get(self, user_id):
        """
        List roles attached to a user.
        """

        user = User.query.filter_by(id=user_id).first()
        if user:
            return user.to_dict()["roles"]
        else:
            abort(404, "User ID not found")

    @api.expect(aaa_set_user_roles)
    def post(self, user_id):
        """
        Set roles for a user.
        """
        user = User.query.filter_by(id=user_id).first()
        if user:
            log.info(request.json.get("roles"))
            log.info(user.to_dict())
            for role in request.json.get("roles"):
                db_role = JinjamatorRole.query.filter_by(name=role).first()

                user.roles.merge()

            log.info(user.to_dict())
            user.commit()

            user.refresh()

            return user.to_dict()["roles"]
        else:
            abort(404, "User ID not found")


@ns.route("/roles")
class Roles(Resource):
    def get(self):
        """
        List available user roles.
        """
        retval = []
        for role in JinjamatorRole.query.all():
            del role.users
            retval.append(role.to_dict())
        return retval

    @api.expect(aaa_create_role)
    def post(self):
        """
        Create a new role.
        """
        try:
            new_role = JinjamatorRole(name=request.json["name"])
        except IndexError:
            abort(400, "Parameters missing, or not properly encoded")
        db.session.add(new_role)
        try:
            db.session.commit()
            db.session.refresh(new_role)
            return new_role.to_dict()
        except:
            abort(400, "User exists")


@ns.route("/roles/<role_id>")
class RoleDetail(Resource):
    @api.doc(params={"role_id": "The User ID of the role which should be returned"})
    def get(self, user_id):
        """
        Get detailed information about a role.
        """
        role = JinjamatorRole.query.filter_by(id=user_id).first()
        if role:
            return role
        else:
            abort(404, "JinjamatorRole ID not found")
