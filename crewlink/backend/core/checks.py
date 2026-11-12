"""
Startup guard for default-deny. Runs with `manage.py check`, `runserver` and the test
runner, so a new endpoint with no declared policy stops the app from starting instead
of silently shipping.

  core.E001  route has no explicit permission_classes
  core.E002  route uses AllowAny but is not on the PUBLIC_ROUTES allow-list
  core.E003  DEFAULT_PERMISSION_CLASSES is not [DenyAll]
  core.E004  route is not a DRF view, so default-deny cannot apply to it
"""
from django.conf import settings
from django.core.checks import Error, register
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

PUBLIC_ROUTES = {"api/health/", "api/auth/login/"}


def _walk(patterns, prefix=""):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from _walk(pattern.url_patterns, prefix + str(pattern.pattern))
        elif isinstance(pattern, URLPattern):
            yield prefix + str(pattern.pattern), pattern.callback


@register()
def check_endpoint_permissions(app_configs=None, **kwargs):
    errors = []
    default = settings.REST_FRAMEWORK.get("DEFAULT_PERMISSION_CLASSES")
    if list(default or []) != ["core.permissions.DenyAll"]:
        errors.append(Error("DEFAULT_PERMISSION_CLASSES must be ['core.permissions.DenyAll'].", id="core.E003"))

    for route, callback in _walk(get_resolver().url_patterns):
        view_cls = getattr(callback, "cls", None)
        if view_cls is None or not issubclass(view_cls, APIView):
            errors.append(Error(f"{route}: not a DRF view, so default-deny does not apply.", id="core.E004"))
        elif view_cls.permission_classes is APIView.permission_classes:
            errors.append(Error(f"{route}: no explicit permission_classes declared.", id="core.E001"))
        elif AllowAny in view_cls.permission_classes and route not in PUBLIC_ROUTES:
            errors.append(Error(f"{route}: AllowAny on a route that is not in PUBLIC_ROUTES.", id="core.E002"))
    return errors
