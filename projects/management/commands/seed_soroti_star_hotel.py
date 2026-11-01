"""Create the starting data for the Soroti Star Hotel project (safe to run more than once).

    python manage.py seed_soroti_star_hotel

It adds units, material categories and a starter materials register, the
Soroti Star Hotel project, its 25 construction components and the hotel
facilities. Budgets, quantities and prices are NOT invented here: enter them
from the approved BOQ when it is available.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.models import Material, MaterialCategory, Unit
from projects.models import Phase, Project

UNITS = [
    ("Bag", "bag"), ("Piece", "pc"), ("Kilogram", "kg"), ("Tonne", "t"), ("Metre", "m"),
    ("Square metre", "m2"), ("Cubic metre", "m3"), ("Litre", "L"), ("Roll", "roll"),
    ("Set", "set"), ("Trip", "trip"), ("Sheet", "sheet"),
]

# (code, name, category, unit abbreviation, reorder level)
MATERIALS = [
    ("CEM-TOR", "Tororo Cement 50kg", "Cement", "bag", 50),
    ("CEM-HIM", "Hima Cement 50kg", "Cement", "bag", 50),
    ("STL-REB", "Reinforcement bars", "Steel", "pc", 50),
    ("STL-WIRE", "Binding wire", "Steel", "kg", 20),
    ("AGG-SAND", "Sand", "Aggregates", "trip", 3),
    ("AGG-HARD", "Hardcore", "Aggregates", "trip", 3),
    ("AGG-STONE", "Aggregates (crushed stone)", "Aggregates", "trip", 3),
    ("ROOF-IRON", "Iron sheets", "Roofing", "sheet", 50),
    ("TIM-PLANK", "Timber planks", "Timber", "pc", 50),
    ("MAS-BLOCK", "Building blocks", "Masonry", "pc", 500),
    ("PLB-PIPE", "Plumbing pipes", "Plumbing", "pc", 20),
    ("ELE-CABLE", "Electrical cable", "Electrical", "m", 100),
    ("FIN-TILE", "Floor tiles", "Finishing", "m2", 50),
    ("FIN-PAINT", "Paint", "Finishing", "L", 20),
    ("HRD-NAIL", "Nails", "Hardware", "kg", 10),
    ("SAN-WC", "Toilet / WC set", "Sanitary", "set", 5),
]

CONSTRUCTION = [
    "Site preparation", "Excavation", "Foundation", "Ground floor", "Upper floors",
    "Structural works", "Roofing", "Plumbing", "Electrical installation", "HVAC and ventilation",
    "Plastering", "Flooring", "Wall finishing", "Painting", "Doors and windows",
    "Ceiling installation", "Fire safety installations", "External works", "Landscaping",
    "Interior furnishing", "Kitchen installation", "Swimming pool", "Lifts and elevators",
    "External drainage and sanitation", "Final testing and commissioning",
]

FACILITIES = [
    "Guest rooms fit-out", "Reception and lobby", "Restaurant", "Bar and lounge",
    "Conference hall and meeting rooms", "Gym and spa", "Business centre", "Commercial laundry",
    "Secure parking", "CCTV and access control", "Standby generator and solar provision",
    "Water storage and treatment", "Accessibility provisions", "Pre-opening preparation",
]


class Command(BaseCommand):
    help = "Seed units, a starter materials register and the Soroti Star Hotel project."

    @transaction.atomic
    def handle(self, *args, **options):
        units = {}
        for name, abbr in UNITS:
            units[abbr], _ = Unit.objects.get_or_create(name=name, defaults={"abbreviation": abbr})

        for code, name, cat, unit_abbr, reorder in MATERIALS:
            category, _ = MaterialCategory.objects.get_or_create(name=cat)
            Material.objects.get_or_create(
                code=code,
                defaults={"name": name, "category": category, "unit": units[unit_abbr],
                          "reorder_level": Decimal(reorder)},
            )

        project, created = Project.objects.get_or_create(
            code="SSH-001",
            defaults={
                "name": "Soroti Star Hotel", "city": "Soroti", "district": "Soroti",
                "location": "Soroti City, Uganda",
                "star_target": Project.StarTarget.FOUR_PLUS,
                "status": Project.Status.PLANNING,
                "description": "4-star hotel with selected 5-star facilities and provision for future expansion.",
            },
        )

        seq = 0
        for name in CONSTRUCTION:
            seq += 1
            Phase.objects.get_or_create(project=project, name=name,
                                        defaults={"sequence": seq, "kind": Phase.Kind.CONSTRUCTION})
        for name in FACILITIES:
            seq += 1
            Phase.objects.get_or_create(project=project, name=name,
                                        defaults={"sequence": seq, "kind": Phase.Kind.FACILITY})

        self.stdout.write(self.style.SUCCESS(
            f"{'Created' if created else 'Found'} project {project}; "
            f"{project.phases.count()} components/facilities, {Material.objects.count()} materials."
        ))
