"""One-time import: turn the employee details already typed into assets into onboarded Employee records."""


def import_employees_from_assets(Employee, Asset):
    """Creates one Employee per distinct asset email (and per reporting person), links the assets, sets reporting lines.

    Employee IDs are generated as EMP-0001, EMP-0002, ... and can be edited in the admin app afterwards.
    Safe to run twice: existing employees are matched by email and never duplicated or overwritten.
    """
    used = set(Employee.objects.values_list("employee_id", flat=True))
    counter = [0]

    def next_id():
        while True:
            counter[0] += 1
            candidate = f"EMP-{counter[0]:04d}"
            if candidate not in used:
                used.add(candidate)
                return candidate

    def person(email, name, department=""):
        email = (email or "").strip()
        if not email:
            return None
        found = Employee.objects.filter(email__iexact=email).first()
        if found:
            return found
        return Employee.objects.create(employee_id=next_id(), name=(name or email.split("@")[0])[:120], email=email, department=(department or "")[:120])

    created = 0
    before = Employee.objects.count()
    for a in Asset.objects.filter(employee__isnull=True).order_by("asset_id"):
        emp = person(a.employee_email, a.employee_name, a.department)
        if not emp:
            continue
        a.employee = emp
        a.save(update_fields=["employee"])
        boss = person(a.manager_email, a.manager_name)
        if boss and boss.pk != emp.pk and emp.manager_id is None:
            emp.manager = boss
            emp.save(update_fields=["manager"])
    created = Employee.objects.count() - before
    return created
