from django.core.management.base import BaseCommand, CommandError

from pulse import mockdata
from pulse.models import Asset


class Command(BaseCommand):
    help = ("Create a month of realistic mock data: eight people with app usage, tasks, appreciation and weekend work that end today. "
            "Safe to re-run (it refreshes). Use --asset to add mock TASKS and appreciation to a real laptop (its app usage is never faked).")

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="How many days of history (default 30).")
        parser.add_argument("--asset", action="append", default=[], help="Asset id to seed history for (repeatable). Without it, mock people are created.")
        parser.add_argument("--few", action="store_true", help="Just 3 people with about 6 tasks and 3 appreciation items each: a small, tidy demo.")
        parser.add_argument("--remove", action="store_true", help="Remove the mock people created by this command (never touches real assets).")

    def handle(self, *args, **opts):
        if opts["remove"]:
            self.stdout.write(self.style.SUCCESS(f"Removed {mockdata.remove_mock()} mock people."))
            return
        days = max(7, min(opts["days"], 120))
        assets = None
        if opts["asset"]:
            assets = list(Asset.objects.filter(asset_id__in=opts["asset"]))
            missing = set(opts["asset"]) - {a.asset_id for a in assets}
            if missing:
                raise CommandError(f"Unknown asset id: {', '.join(sorted(missing))}")
        result = mockdata.seed(days=days, assets=assets, few=opts["few"])
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {result['assets']} people: {result['usage']} usage rows, {result['tasks']} new tasks, "
            f"{result['recognitions']} appreciation items, {result['alerts']} weekend alerts ({days} days ending today)."))
        if not assets:
            self.stdout.write("Mock people use asset ids MOCK001.. and are marked as mock data. Re-run any time to refresh; --remove deletes them.")
