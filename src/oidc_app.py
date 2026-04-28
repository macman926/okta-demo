"""Minimal Flask app demonstrating Okta OIDC login.

This is not part of the lifecycle CLI. Its job is to prove you
understand the OIDC Authorization Code flow end-to-end:

    Browser ---(1) /login----------> Flask
    Flask   ---(2) redirect to----> Okta /authorize
    Okta    ---(3) login UI-------> User
    Okta    ---(4) redirect with--> Flask /authorization-code/callback?code=...
    Flask   ---(5) POST token----->  Okta /token  (with client secret)
    Okta    <--(6) id_token, access_token ---
    Flask   ---(7) validate id_token (signature + claims via JWKS)
    Flask   ---(8) set session -----> Browser

Run:
    export FLASK_APP=src/oidc_app.py
    flask --app src/oidc_app run --port 5000

Then open http://localhost:5000.

Authlib handles JWKS fetching, id_token signature + claims validation,
and PKCE. We still talk about those explicitly below because that's
what an interviewer will want to hear.
"""

from __future__ import annotations

import secrets
from functools import wraps
from typing import Any, Callable

from authlib.integrations.flask_client import OAuth
from flask import Flask, jsonify, redirect, request, session, url_for

from .config import Settings


def create_app() -> Flask:
    settings = Settings.from_env()
    app = Flask(__name__)
    app.secret_key = settings.flask_secret_key

    oauth = OAuth(app)
    # .well-known/openid-configuration does the heavy lifting:
    # endpoints + JWKS URI are discovered automatically.
    oauth.register(
        name="okta",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=f"{settings.oidc_issuer}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email"},
    )

    def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if "user" not in session:
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped

    @app.route("/")
    def home() -> Any:
        if "user" in session:
            return (
                f"<h1>Hello, {session['user'].get('name')!s}</h1>"
                f"<p>email: {session['user'].get('email')!s}</p>"
                '<p><a href="/profile">Profile (JSON)</a> · '
                '<a href="/logout">Logout</a></p>'
            )
        return '<h1>hrt-okta-demo</h1><p><a href="/login">Log in with Okta</a></p>'

    @app.route("/login")
    def login() -> Any:
        # `nonce` protects against replay of id_tokens. Authlib stores
        # and validates it on callback automatically.
        nonce = secrets.token_urlsafe(16)
        session["oidc_nonce"] = nonce
        redirect_uri = settings.oidc_redirect_uri
        return oauth.okta.authorize_redirect(redirect_uri, nonce=nonce)

    @app.route("/authorization-code/callback")
    def callback() -> Any:
        token = oauth.okta.authorize_access_token()
        nonce = session.pop("oidc_nonce", None)
        user = oauth.okta.parse_id_token(token, nonce=nonce)
        session["user"] = {
            "sub": user.get("sub"),
            "name": user.get("name"),
            "email": user.get("email"),
            "groups": user.get("groups", []),
        }
        return redirect(url_for("home"))

    @app.route("/profile")
    @login_required
    def profile() -> Any:
        return jsonify(session["user"])

    @app.route("/logout")
    def logout() -> Any:
        # Local session logout. For a full SLO you'd also redirect the
        # browser to {issuer}/v1/logout?id_token_hint=...&post_logout_redirect_uri=...
        session.clear()
        return redirect(url_for("home"))

    return app


# `flask --app src/oidc_app run` looks for a module-level `app` or a
# factory called `create_app`. Exposing `app` is friendlier for `python
# -m src.oidc_app` one-liners too.
app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
