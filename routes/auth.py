from flask import Blueprint, render_template, request, flash, redirect, url_for, session
from utils import auth_enabled, auth_username, auth_password, is_authenticated

auth_bp = Blueprint("auth", __name__)

@auth_bp.before_app_request
def require_login_when_configured():
    if not auth_enabled():
        return None

    allowed_endpoints = {
        "healthz",
        "auth.login",
        "auth.logout",
        "static",
    }
    if request.endpoint in allowed_endpoints:
        return None

    if is_authenticated():
        return None

    return redirect(url_for("auth.login", next=request.path))

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if not auth_enabled():
        return redirect(url_for("main.index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        next_url = request.form.get("next", "").strip()
        if username == auth_username() and password == auth_password():
            session["authenticated"] = True
            flash("Signed in.")
            return redirect(next_url or url_for("main.index"))
        flash("Login failed. Check the username and password.")

    next_url = request.values.get("next", "").strip()
    return render_template("login.html", next_url=next_url, username=auth_username())

@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.pop("authenticated", None)
    flash("Signed out.")
    return redirect(url_for("auth.login"))