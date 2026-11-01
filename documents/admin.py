from django.contrib import admin

from documents.models import Attachment


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    """Browse only. Files are added from the site pages (/files/upload/)."""

    list_display = ("caption", "kind", "project", "diary", "purchase_order", "created_by", "created_at")
    list_filter = ("kind", "project")
    search_fields = ("caption",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
