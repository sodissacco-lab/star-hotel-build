"""Load the starter list into a project:  python manage.py load_starter <project code>"""
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from core.starter import load_starter_data
from projects.models import Project


class Command(BaseCommand):
    help = "Add the starter units, categories, materials and building components to a project."

    def add_arguments(self, parser):
        parser.add_argument("code", help="project code")

    def handle(self, *args, **options):
        project = Project.objects.filter(code=options["code"]).first()
        if not project:
            raise CommandError("No project with that code.")
        admin = User.objects.filter(is_superuser=True).first()
        if not admin:
            raise CommandError("There is no superuser to run this as.")
        load_starter_data(project, admin)
        self.stdout.write(self.style.SUCCESS(f"Added: {project._starter_added}"))
