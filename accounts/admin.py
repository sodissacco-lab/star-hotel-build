from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from accounts.models import User


@admin.register(User)
class StarBuildUserAdmin(UserAdmin):
    list_display = ("username", "get_full_name", "role", "phone", "is_active", "last_login")
    list_filter = ("role", "is_active", "is_staff")
    fieldsets = UserAdmin.fieldsets + (("StarBuild", {"fields": ("role", "phone")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("StarBuild", {"fields": ("role", "phone")}),)
