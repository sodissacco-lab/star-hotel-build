"""Role -> Django admin access.

Every user whose role is not Contractor is staff, and belongs to one permission group named
after their role ("StarBuild: Storekeeper"). The group decides which records they may see,
add and change in the admin. Approvals are still controlled separately by the capability
rules in accounts/permissions.py (and segregation of duties), so admin access never lets
someone approve their own work.
"""
from django.apps import apps
from django.contrib.auth.models import Group, Permission

from accounts.models import User

R = User.Role
APPS = ["projects", "inventory", "finance", "boq", "procurement", "workforce", "documents"]
PREFIX = "StarBuild: "

OPS = ["project", "phase", "workpackage", "sitediary", "siteactivity", "attachment"]
STORES = ["materialcategory", "unit", "supplier", "material", "purchaseorder", "purchaseorderline", "goodsreceipt",
          "goodsreceiptline", "materialissue", "materialissueline", "materialreturn", "wastagerecord",
          "stockadjustment", "stockledger"]
BOQ = ["boqitem", "boqmeasurement", "variation"]
PROC = ["requisition", "requisitionline", "quotation"]
FIN = ["budget", "expense", "payment", "demandnote", "receipt"]
LABOUR = ["contractor", "contract", "paymentcertificate", "worker", "attendance", "payroll", "payrollline",
          "contractoradvance", "workeradvance"]
EVERYTHING = OPS + STORES + BOQ + PROC + FIN + LABOUR

# role -> (models they can view, models they can add and change)
ACCESS = {
    R.OWNER: (EVERYTHING, []),
    R.AUDITOR: (EVERYTHING, []),
    R.PROJECT_MANAGER: (EVERYTHING, OPS + BOQ + PROC + ["purchaseorder", "purchaseorderline", "goodsreceipt", "goodsreceiptline",
                                                        "materialissue", "materialissueline", "demandnote", "contractor",
                                                        "contract", "paymentcertificate", "worker", "attendance", "contractoradvance", "workeradvance"]),
    R.SITE_ENGINEER: (OPS + BOQ + STORES + PROC + ["worker", "attendance"],
                      ["sitediary", "siteactivity", "requisition", "requisitionline", "boqmeasurement",
                       "wastagerecord", "attendance"]),
    R.QUANTITY_SURVEYOR: (OPS + BOQ + STORES + PROC + FIN + LABOUR,
                          BOQ + ["requisition", "requisitionline", "quotation", "demandnote", "contractoradvance"]),
    R.PROCUREMENT: (OPS + STORES + PROC + ["demandnote"],
                    ["supplier", "material", "materialcategory", "unit", "purchaseorder", "purchaseorderline",
                     "quotation", "demandnote"]),
    R.ACCOUNTANT: (OPS + STORES + PROC + BOQ + FIN + LABOUR, ["budget", "expense", "payment", "demandnote", "receipt", "workeradvance"]),
    R.STOREKEEPER: (OPS + STORES + PROC, ["goodsreceipt", "goodsreceiptline", "materialissue", "materialissueline",
                                          "materialreturn", "wastagerecord"]),
    R.SUPERVISOR: (OPS + ["worker", "attendance", "wastagerecord", "material", "unit", "materialcategory"],
                   ["sitediary", "siteactivity", "attendance", "wastagerecord"]),
    R.HR: (OPS + LABOUR, ["worker", "attendance", "payroll", "payrollline", "workeradvance"]),
    R.CONTRACTOR: ([], []),
}


def all_model_names():
    return [m._meta.model_name for app in APPS for m in apps.get_app_config(app).get_models()]


def wanted_permission_ids(role):
    if role == R.ADMIN:
        names = all_model_names()
        codenames = {f"{a}_{n}" for n in names for a in ("view", "add", "change", "delete")}
    else:
        view, edit = ACCESS.get(role, ([], []))
        codenames = {f"view_{n}" for n in view} | {f"{a}_{n}" for n in edit for a in ("add", "change")} \
            | {f"view_{n}" for n in edit}
    if not codenames:
        return set()
    return set(Permission.objects.filter(content_type__app_label__in=APPS, codename__in=codenames)
               .values_list("pk", flat=True))


def group_for(role):
    """The role's permission group, created or brought up to date if needed."""
    group, _ = Group.objects.get_or_create(name=PREFIX + R(role).label)
    wanted = wanted_permission_ids(role)
    if set(group.permissions.values_list("pk", flat=True)) != wanted:
        group.permissions.set(wanted)
    return group


def apply_role(user):
    """Put the user in their role's group (and no other StarBuild role group)."""
    if user.is_superuser:
        return
    group = group_for(user.role)
    stale = user.groups.filter(name__startswith=PREFIX).exclude(pk=group.pk)
    if stale.exists():
        user.groups.remove(*stale)
    if not user.groups.filter(pk=group.pk).exists():
        user.groups.add(group)