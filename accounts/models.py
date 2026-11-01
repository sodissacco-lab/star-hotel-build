from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "System Administrator"
        OWNER = "owner", "Project Owner/Director"
        PROJECT_MANAGER = "project_manager", "Project Manager"
        SITE_ENGINEER = "site_engineer", "Site Engineer"
        QUANTITY_SURVEYOR = "quantity_surveyor", "Quantity Surveyor"
        PROCUREMENT = "procurement", "Procurement Officer"
        ACCOUNTANT = "accountant", "Accountant"
        STOREKEEPER = "storekeeper", "Storekeeper"
        SUPERVISOR = "supervisor", "Site Supervisor"
        HR = "hr", "Human Resource Officer"
        CONTRACTOR = "contractor", "Contractor"
        AUDITOR = "auditor", "Auditor (read-only)"

    role = models.CharField(max_length=30, choices=Role.choices, default=Role.SUPERVISOR)
    phone = models.CharField(max_length=30, blank=True)

    def save(self, *args, **kwargs):
        if not self.is_superuser:  # staff status follows the role; contractors never enter the admin
            self.is_staff = self.role != self.Role.CONTRACTOR
        super().save(*args, **kwargs)

    def accessible_projects(self):
        """Projects this user may see. Admin, Owner and Auditor see all;
        everyone else sees only projects they were added to as a member."""
        from projects.models import Project

        if self.is_superuser or self.role in (self.Role.ADMIN, self.Role.OWNER, self.Role.AUDITOR):
            return Project.objects.all()
        return self.projects.all()

    def __str__(self):
        return self.get_full_name() or self.username


@receiver(post_save, sender=User)
def sync_role_access(sender, instance, created, raw=False, update_fields=None, **kwargs):
    """Keep the user's permission group in step with their role."""
    if raw or (update_fields is not None and "role" not in update_fields):
        return
    from accounts.access import apply_role
    apply_role(instance)