from rest_framework.permissions import BasePermission

from accounts.permissions import can


class HasCapability(BasePermission):
    """Use as: permission_classes = [IsAuthenticated, capability("view_boq")]."""

    capability_name = None

    def has_permission(self, request, view):
        return can(request.user, self.capability_name)


def capability(name):
    return type(f"Has_{name}", (HasCapability,), {"capability_name": name, "message": f"Your role is not permitted ({name.replace('_', ' ')})."})
