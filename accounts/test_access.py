from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from accounts.models import User

R = User.Role


def make(name, role):
    return User.objects.create_user(name, password="x" * 12, role=role)


def perms(user):
    return User.objects.get(pk=user.pk)  # fresh copy: Django caches permissions on the instance


class RoleAccessTests(TestCase):
    def test_staff_follows_role(self):
        self.assertTrue(make("store", R.STOREKEEPER).is_staff)
        self.assertFalse(make("sub", R.CONTRACTOR).is_staff)
        root = User.objects.create_superuser("root", password="x" * 12)
        self.assertTrue(root.is_staff)

    def test_storekeeper_permissions(self):
        u = perms(make("store", R.STOREKEEPER))
        self.assertTrue(u.has_perm("inventory.add_goodsreceipt"))
        self.assertTrue(u.has_perm("inventory.add_materialissue"))
        self.assertTrue(u.has_perm("inventory.view_purchaseorder"))
        self.assertFalse(u.has_perm("inventory.add_purchaseorder"))
        self.assertFalse(u.has_perm("finance.view_payment"))
        self.assertFalse(u.has_perm("workforce.view_payroll"))

    def test_accountant_and_auditor(self):
        acc, aud = perms(make("acc", R.ACCOUNTANT)), perms(make("aud", R.AUDITOR))
        self.assertTrue(acc.has_perm("finance.add_payment"))
        self.assertTrue(acc.has_perm("finance.change_receipt"))
        self.assertFalse(acc.has_perm("inventory.add_goodsreceipt"))
        self.assertTrue(aud.has_perm("finance.view_expense"))
        self.assertFalse(aud.has_perm("finance.add_expense"))
        self.assertFalse(aud.has_perm("inventory.change_material"))

    def test_admin_role_has_everything_and_contractor_nothing(self):
        adm, con = perms(make("adm", R.ADMIN)), perms(make("con", R.CONTRACTOR))
        self.assertTrue(adm.has_perm("finance.delete_expense"))
        self.assertTrue(adm.has_perm("projects.add_project"))
        self.assertFalse(con.has_perm("projects.view_project"))

    def test_changing_role_changes_access(self):
        u = make("mover", R.STOREKEEPER)
        u.role = R.HR
        u.save()
        u = perms(u)
        self.assertTrue(u.has_perm("workforce.add_payroll"))
        self.assertFalse(u.has_perm("inventory.add_goodsreceipt"))
        self.assertEqual(u.groups.filter(name__startswith="StarBuild: ").count(), 1)

    def test_login_style_save_does_not_touch_groups(self):
        u = make("quick", R.SITE_ENGINEER)
        before = list(u.groups.values_list("pk", flat=True))
        u.save(update_fields=["last_login"])
        self.assertEqual(list(u.groups.values_list("pk", flat=True)), before)

    def test_admin_site_access(self):
        store = make("store", R.STOREKEEPER)
        self.client.force_login(store)
        self.assertEqual(self.client.get("/admin/inventory/goodsreceipt/").status_code, 200)
        self.assertEqual(self.client.get("/admin/finance/payment/").status_code, 403)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_sync_roles_command_repairs_existing_users(self):
        u = make("old", R.SUPERVISOR)
        User.objects.filter(pk=u.pk).update(is_staff=False)
        u.groups.clear()
        call_command("sync_roles", stdout=StringIO())
        u = perms(u)
        self.assertTrue(u.is_staff)
        self.assertTrue(u.has_perm("projects.add_sitediary"))
