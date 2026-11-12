from rest_framework.permissions import BasePermission

from .models import Account


class DenyAll(BasePermission):
    """Project default. A view that forgets to declare permission_classes gets this."""

    def has_permission(self, request, view):
        return False


class IsAccount(BasePermission):
    """Any authenticated, active account (member functionality; leaders are members too)."""

    def has_permission(self, request, view):
        user = request.user
        return isinstance(user, Account) and user.is_active


class IsLeader(IsAccount):
    """Leadership endpoints. The role is read from the database on every request
    (the token only identifies the account), so a demotion takes effect immediately."""

    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.role == Account.Role.LEADER
