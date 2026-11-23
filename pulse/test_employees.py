from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Asset, Employee


class EmployeeOnboardingTests(TestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_user("admin", password="s3cret-pass", is_staff=True)
        self.api = APIClient()
        token = self.api.post("/api/admin/login", {"username": "admin", "password": "s3cret-pass"}, format="json").json()["token"]
        self.api.credentials(HTTP_AUTHORIZATION=f"Token {token}")

    def onboard(self, **kw):
        body = {"employeeId": "E001", "name": "Raj Rao", "email": "raj@x.com", "department": "Engineering"}
        body.update(kw)
        return self.api.post("/api/admin/employees", body, format="json")

    def test_requires_admin(self):
        self.assertEqual(APIClient().get("/api/admin/employees").status_code, 401)

    def test_onboard_with_reporting_person_and_validation(self):
        raj = self.onboard().json()
        r = self.onboard(employeeId="E002", name="Asha Rao", email="asha@x.com", managerId=raj["id"])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["managerName"], "Raj Rao")
        self.assertEqual(r.json()["managerEmployeeId"], "E001")
        self.assertEqual(self.onboard(employeeId="e001", email="other@x.com").status_code, 400, "employee ID unique, case-insensitive")
        self.assertEqual(self.onboard(employeeId="E009", email="RAJ@x.com").status_code, 400, "email unique")
        self.assertEqual(self.onboard(employeeId="E010", email="bad").status_code, 400)
        self.assertEqual(self.onboard(employeeId="E011", email="n@x.com", name="").status_code, 400)
        self.assertEqual(self.onboard(employeeId="E012", email="m@x.com", managerId=9999).status_code, 400)
        self.assertEqual(len(self.api.get("/api/admin/employees").json()), 2)

    def test_no_reporting_loops(self):
        raj = self.onboard().json()
        asha = self.onboard(employeeId="E002", email="asha@x.com", managerId=raj["id"]).json()
        self.assertEqual(self.api.patch(f"/api/admin/employees/{raj['id']}", {"managerId": raj["id"]}, format="json").status_code, 400)
        self.assertEqual(self.api.patch(f"/api/admin/employees/{raj['id']}", {"managerId": asha["id"]}, format="json").status_code, 400)

    def test_asset_takes_details_and_reporting_person_from_employee(self):
        raj = self.onboard().json()
        asha = self.onboard(employeeId="E002", name="Asha Rao", email="asha@x.com", department="Design", managerId=raj["id"]).json()
        r = self.api.post("/api/admin/assets", {"assetId": "PGL-1", "employeeRef": asha["id"], "location": "Chennai"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        a = r.json()
        self.assertEqual((a["employeeName"], a["employeeEmail"], a["department"], a["employeeCode"]), ("Asha Rao", "asha@x.com", "Design", "E002"))
        self.assertEqual(Asset.objects.get(pk="PGL-1").manager_email, "raj@x.com", "reporting person copied for Orbit")

    def test_asset_ignores_reporting_person_typed_in_the_request(self):
        raj = self.onboard().json()
        r = self.api.post("/api/admin/assets", {"assetId": "PGL-2", "employeeRef": raj["id"], "managerName": "Someone Else", "managerEmail": "x@y.com"}, format="json")
        self.assertEqual(Asset.objects.get(pk="PGL-2").manager_name, "")
        self.assertEqual(r.status_code, 201)

    def test_unknown_or_inactive_employee_is_rejected(self):
        raj = self.onboard().json()
        self.assertEqual(self.api.post("/api/admin/assets", {"assetId": "PGL-3", "employeeRef": 9999}, format="json").status_code, 400)
        self.api.patch(f"/api/admin/employees/{raj['id']}", {"active": False}, format="json")
        self.assertEqual(self.api.post("/api/admin/assets", {"assetId": "PGL-3", "employeeRef": raj["id"]}, format="json").status_code, 400)

    def test_changing_the_manager_updates_assigned_assets(self):
        raj = self.onboard().json()
        meera = self.onboard(employeeId="E003", name="Meera Das", email="meera@x.com").json()
        asha = self.onboard(employeeId="E002", name="Asha Rao", email="asha@x.com", managerId=raj["id"]).json()
        self.api.post("/api/admin/assets", {"assetId": "PGL-4", "employeeRef": asha["id"]}, format="json")
        self.api.patch(f"/api/admin/employees/{asha['id']}", {"managerId": meera["id"]}, format="json")
        self.assertEqual(Asset.objects.get(pk="PGL-4").manager_name, "Meera Das")
        self.api.patch(f"/api/admin/employees/{meera['id']}", {"name": "Meera D"}, format="json")
        self.assertEqual(Asset.objects.get(pk="PGL-4").manager_name, "Meera D", "renaming the manager reaches their reports' assets")

    def test_cannot_delete_employee_with_an_asset(self):
        raj = self.onboard().json()
        self.api.post("/api/admin/assets", {"assetId": "PGL-5", "employeeRef": raj["id"]}, format="json")
        self.assertEqual(self.api.delete(f"/api/admin/employees/{raj['id']}").status_code, 409)
        self.api.delete("/api/admin/assets/PGL-5")
        self.assertEqual(self.api.delete(f"/api/admin/employees/{raj['id']}").status_code, 204)
        self.assertFalse(Employee.objects.exists())


class ImportFromAssetsTests(TestCase):
    def test_existing_assets_become_employees_with_reporting_lines(self):
        from .employees_import import import_employees_from_assets
        Asset.objects.create(asset_id="A1", employee_name="Asha Rao", employee_email="asha@x.com", department="Design", manager_name="Raj Rao", manager_email="raj@x.com")
        Asset.objects.create(asset_id="A2", employee_name="Asha Rao", employee_email="ASHA@x.com")  # second laptop, same person
        Asset.objects.create(asset_id="A3", employee_name="Raj Rao", employee_email="raj@x.com")
        self.assertEqual(import_employees_from_assets(Employee, Asset), 2)
        asha, raj = Employee.objects.get(email="asha@x.com"), Employee.objects.get(email="raj@x.com")
        self.assertEqual(asha.manager, raj)
        self.assertEqual({a.employee_id for a in Asset.objects.all()}, {asha.pk, raj.pk})
        self.assertEqual(asha.assets.count(), 2)
        self.assertEqual(import_employees_from_assets(Employee, Asset), 0, "running twice creates nothing new")
