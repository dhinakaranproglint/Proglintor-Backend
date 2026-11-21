"""Sign-in, first-run setup and password reset for the Proglintor Admin desktop app.

    POST /api/admin/login            {login, password}            login = username or e-mail
    GET  /api/admin/setup            -> {needsSetup, allowed}
    POST /api/admin/setup            {name, username, email, password}   first admin, from this computer only
    POST /api/admin/password/forgot  {login}                      e-mails a 6-digit code (always answers the same)
    POST /api/admin/password/reset   {login, code, password}      sets a new password
"""
import hashlib
import ipaddress
import secrets
import time
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import permissions
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PasswordResetCode

User = get_user_model()


def _ip(request):
    return request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() or request.META.get("REMOTE_ADDR", "")


def _is_local(request):
    """True when the request comes from the backend computer itself (and not through a proxy)."""
    if request.META.get("HTTP_X_FORWARDED_FOR"):
        return False
    try:
        return ipaddress.ip_address(request.META.get("REMOTE_ADDR", "")).is_loopback
    except ValueError:
        return False


def _limited(key, limit, window):
    now = time.time()
    hits = [t for t in cache.get(key, []) if now - t < window]
    hits.append(now)
    cache.set(key, hits, window)
    return len(hits) > limit


def _hash(code):
    return hashlib.sha256(code.encode()).hexdigest()


def _find_staff(login):
    login = str(login or "").strip()
    if not login:
        return None
    return User.objects.filter(Q(username__iexact=login) | Q(email__iexact=login), is_staff=True, is_active=True).first()


def _password_errors(password, user=None):
    try:
        validate_password(password, user=user)
        return None
    except ValidationError as e:
        return " ".join(e.messages)


def _signed_in(user):
    token, _ = Token.objects.get_or_create(user=user)
    return {"token": token.key, "user": {"username": user.get_username(), "name": user.get_full_name() or user.get_username(), "email": user.email}}


class PublicView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]


class LoginView(PublicView):
    def post(self, request):
        if _limited(f"login:{_ip(request)}", 20, 600):
            return Response({"error": "Too many sign-in attempts. Please wait a few minutes and try again."}, status=429)
        login = str(request.data.get("login") or request.data.get("username") or "").strip()
        password = request.data.get("password") or ""
        if not login or not password:
            return Response({"error": "Enter your username or e-mail and your password.", "field": "login" if not login else "password"}, status=400)
        if not User.objects.filter(is_staff=True).exists():
            return Response({"error": "No admin account exists yet. Create the first one to get started.", "needsSetup": True}, status=400)
        candidate = _find_staff(login) or User.objects.filter(username__iexact=login).first()
        user = authenticate(request, username=candidate.get_username(), password=password) if candidate else None
        if not user or not user.is_active:
            return Response({"error": "That username/e-mail and password don't match. Check them, or use “Forgot password?”.", "field": "password"}, status=400)
        if not user.is_staff:
            return Response({"error": "This account isn't allowed to use Proglintor Admin. Ask an administrator for access."}, status=403)
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        return Response(_signed_in(user))


class SetupView(PublicView):
    """Lets the very first admin be created from the desktop app (only on the backend computer)."""

    def get(self, request):
        return Response({"needsSetup": not User.objects.filter(is_staff=True).exists(), "allowed": _is_local(request)})

    def post(self, request):
        if User.objects.filter(is_staff=True).exists():
            return Response({"error": "An admin account already exists. Sign in instead."}, status=409)
        if not _is_local(request):
            return Response({"error": "For security, the first admin can only be created on the computer running the backend "
                                      "(or with: python manage.py createsuperuser)."}, status=403)
        name = str(request.data.get("name") or "").strip()
        email = str(request.data.get("email") or "").strip()
        username = str(request.data.get("username") or "").strip() or email  # e-mail sign-in: e-mail doubles as username
        password = request.data.get("password") or ""
        if not username or not User.username_validator.regex.match(username):
            return Response({"error": "Choose a username (letters, numbers and @ . + - _ only).", "field": "username"}, status=400)
        try:
            validate_email(email)
        except ValidationError:
            return Response({"error": "Enter a valid e-mail address — it's used if you forget your password.", "field": "email"}, status=400)
        errors = _password_errors(password, User(username=username, email=email))
        if errors:
            return Response({"error": errors, "field": "password"}, status=400)
        first, _, last = name.partition(" ")
        with transaction.atomic():
            if User.objects.select_for_update().filter(is_staff=True).exists():
                return Response({"error": "An admin account already exists. Sign in instead."}, status=409)
            user = User.objects.create_superuser(username=username, email=email, password=password, first_name=first[:150], last_name=last[:150])
        return Response(_signed_in(user), status=201)


GENERIC_SENT = "If an admin account matches, a 6-digit code has been sent to its e-mail address. It expires in {m} minutes."


class ForgotPasswordView(PublicView):
    def post(self, request):
        login = str(request.data.get("login") or "").strip()
        if not login:
            return Response({"error": "Enter your username or e-mail.", "field": "login"}, status=400)
        if _limited(f"forgot:{_ip(request)}", 5, 900) or _limited(f"forgot-login:{login.lower()}", 3, 900):
            return Response({"error": "Too many requests. Please wait 15 minutes before asking for another code."}, status=429)
        minutes = settings.PASSWORD_RESET_MINUTES
        user = _find_staff(login)
        if user and user.email:
            code = f"{secrets.randbelow(1_000_000):06d}"
            PasswordResetCode.objects.filter(user=user, used=False).update(used=True)  # only the newest code works
            PasswordResetCode.objects.create(user=user, code_hash=_hash(code), expires_at=timezone.now() + timedelta(minutes=minutes))
            send_mail(
                subject="Your Proglintor Admin password reset code",
                message=(f"Hi {user.get_full_name() or user.get_username()},\n\n"
                         f"Your code to reset your Proglintor Admin password is: {code}\n\n"
                         f"It expires in {minutes} minutes. If you didn't ask for this, you can ignore this e-mail.\n\n— Proglintor"),
                from_email=None,
                recipient_list=[user.email],
                fail_silently=False,
            )
        return Response({"ok": True, "message": GENERIC_SENT.format(m=minutes)})


class ResetPasswordView(PublicView):
    def post(self, request):
        login = str(request.data.get("login") or "").strip()
        code = "".join(ch for ch in str(request.data.get("code") or "") if ch.isdigit())
        password = request.data.get("password") or ""
        if _limited(f"reset:{_ip(request)}", 15, 900):
            return Response({"error": "Too many attempts. Please wait 15 minutes."}, status=429)
        user = _find_staff(login)
        rc = PasswordResetCode.objects.filter(user=user, used=False).first() if user else None
        if not rc or not rc.valid:
            return Response({"error": "This code has expired or is no longer valid. Request a new one.", "field": "code"}, status=400)
        if len(code) != 6 or not secrets.compare_digest(rc.code_hash, _hash(code)):
            rc.attempts += 1
            rc.save(update_fields=["attempts"])
            left = max(0, 5 - rc.attempts)
            msg = f"That code isn't right. {left} attempt{'s' if left != 1 else ''} left." if left else "Too many wrong codes. Request a new one."
            return Response({"error": msg, "field": "code"}, status=400)
        errors = _password_errors(password, user)
        if errors:
            return Response({"error": errors, "field": "password"}, status=400)
        user.set_password(password)
        user.save()
        rc.used = True
        rc.save(update_fields=["used"])
        Token.objects.filter(user=user).delete()  # sign out everywhere else
        return Response({"ok": True, "message": "Your password has been changed."})
