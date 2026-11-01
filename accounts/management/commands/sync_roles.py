"""Refresh role permission groups and apply them to every user.

Run after upgrading StarBuild (new records get new permissions):  python manage.py sync_roles
"""
from django.core.management.base import BaseCommand

from accounts.access import apply_role, group_for
from accounts.models import User


class Command(BaseCommand):
    help = "Update the role permission groups and give every user the access of their role."

    def handle(self, *args, **options):
        for role in User.Role.values:
            group = group_for(role)
            self.stdout.write(f"{group.name}: {group.permissions.count()} permissions")
        count = 0
        for user in User.objects.all():
            if not user.is_superuser:
                if user.is_staff != (user.role != User.Role.CONTRACTOR):
                    user.save()  # recomputes staff status and group
                else:
                    apply_role(user)
            count += 1
        self.stdout.write(self.style.SUCCESS(f"{count} user(s) updated."))
