from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin

from accounts.models import User


@admin.register(User)
class StarBuildUserAdmin(UserAdmin):
    list_display = ("username", "get_full_name", "role", "phone", "is_active", "last_login")
    list_filter = ("role", "is_active", "is_staff")
    fieldsets = UserAdmin.fieldsets + (("StarBuild", {"fields": ("role", "phone")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("StarBuild", {"fields": ("role", "phone")}),)

    readonly_fields = ("is_staff",)  # follows the role automatically
    actions = ["apply_role_access"]

    @admin.action(description="Re-apply role access to selected users")
    def apply_role_access(self, request, queryset):
        from accounts.access import apply_role
        for user in queryset:
            apply_role(user)
        messages.success(request, f"Access refreshed for {queryset.count()} user(s).")