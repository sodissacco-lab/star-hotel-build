"""Role-based capabilities.

Every approval or posting action in StarBuild checks a named capability, so
that, for example, a storekeeper who records materials received cannot approve
payments. Superusers pass every check.
"""
from accounts.models import User

R = User.Role


class WorkflowError(Exception):
    """A business-rule violation (shown to the user, not a bug)."""


CAPABILITIES = {
    # procurement
    "approve_po": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER},
    "approve_large_po": {R.ADMIN, R.OWNER},
    "verify_requisition": {R.ADMIN, R.QUANTITY_SURVEYOR},
    "approve_requisition": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER},
    "create_po": {R.ADMIN, R.PROCUREMENT, R.PROJECT_MANAGER},
    # goods received without a purchase order: recorded on site (receive_goods), approved here
    "approve_direct_delivery": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER},
    # BOQ
    "approve_variation": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER},
    "manage_boq": {R.ADMIN, R.QUANTITY_SURVEYOR, R.PROJECT_MANAGER},
    "view_boq": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER, R.QUANTITY_SURVEYOR, R.SITE_ENGINEER, R.ACCOUNTANT, R.AUDITOR},
    # site work (also used by the mobile API)
    "raise_requisition": {R.ADMIN, R.SITE_ENGINEER, R.SUPERVISOR, R.STOREKEEPER, R.PROJECT_MANAGER, R.QUANTITY_SURVEYOR, R.PROCUREMENT},
    "record_diary": {R.ADMIN, R.SITE_ENGINEER, R.SUPERVISOR, R.PROJECT_MANAGER},
    "attach_files": {R.ADMIN, R.SITE_ENGINEER, R.SUPERVISOR, R.STOREKEEPER, R.PROJECT_MANAGER, R.QUANTITY_SURVEYOR, R.PROCUREMENT, R.ACCOUNTANT},
    # workforce and contractors
    "manage_workers": {R.ADMIN, R.HR, R.PROJECT_MANAGER},
    "record_attendance": {R.ADMIN, R.SUPERVISOR, R.SITE_ENGINEER, R.HR, R.PROJECT_MANAGER},
    "build_payroll": {R.ADMIN, R.HR},
    "approve_payroll": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER},
    "approve_certificate": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER},
    "approve_mobilisation": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER},
    "approve_worker_advance": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER},
    "view_labour": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER, R.HR, R.ACCOUNTANT, R.QUANTITY_SURVEYOR, R.AUDITOR},
    # stores
    "receive_goods": {R.ADMIN, R.STOREKEEPER, R.PROJECT_MANAGER},
    "issue_materials": {R.ADMIN, R.STOREKEEPER, R.PROJECT_MANAGER},
    "record_wastage": {R.ADMIN, R.STOREKEEPER, R.SITE_ENGINEER, R.SUPERVISOR, R.PROJECT_MANAGER},
    "adjust_stock": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER},
    # finance
    "approve_expense": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER},
    "record_payment": {R.ADMIN, R.ACCOUNTANT},
    "record_demand": {R.ADMIN, R.ACCOUNTANT, R.PROCUREMENT, R.PROJECT_MANAGER},
    "verify_demand": {R.ADMIN, R.QUANTITY_SURVEYOR, R.ACCOUNTANT},
    "approve_demand": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER},
    "approve_advance": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER},
    "approve_large_advance": {R.ADMIN, R.OWNER},
    "issue_receipts": {R.ADMIN, R.ACCOUNTANT},
    "void_receipt": {R.ADMIN, R.ACCOUNTANT, R.PROJECT_MANAGER},
    "view_finance": {R.ADMIN, R.OWNER, R.PROJECT_MANAGER, R.ACCOUNTANT, R.QUANTITY_SURVEYOR, R.AUDITOR},
}


def can(user, capability):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.role in CAPABILITIES.get(capability, set())


def require(user, capability):
    if not can(user, capability):
        raise WorkflowError(f"Your role is not permitted to perform this action ({capability.replace('_', ' ')}).")
