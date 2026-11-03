"""A starter list for a new hotel project: units, material categories, common materials and building components.

Safe to run more than once: anything that already exists (matched by name or code) is left exactly as it is,
so prices and edits you have made are never overwritten."""
from django.db import transaction

from accounts.permissions import WorkflowError, require
from inventory.models import Material, MaterialCategory, Unit
from projects.models import Phase

UNITS = [
    ("Bag", "bag"), ("Kilogram", "kg"), ("Tonne", "t"), ("Piece", "pcs"), ("Metre", "m"), ("Square metre", "m2"),
    ("Cubic metre", "m3"), ("Litre", "l"), ("Tin", "tin"), ("Roll", "roll"), ("Bundle", "bdl"), ("Sheet", "sht"),
    ("Length", "len"), ("Trip", "trip"), ("Set", "set"),
]

CATEGORIES = [
    "Cement and binders", "Aggregates and sand", "Bricks and blocks", "Steel and reinforcement", "Timber", "Roofing",
    "Doors and windows", "Electrical", "Plumbing and sanitary", "Paint and finishes", "Tiles and flooring",
    "Ceiling and partitions", "Waterproofing", "Fuel and lubricants", "Tools and consumables", "Safety equipment",
]

# code, name, category, unit name, reorder level
MATERIALS = [
    ("CEM01", "Cement 50 kg", "Cement and binders", "Bag", 50),
    ("SND01", "River sand", "Aggregates and sand", "Tonne", 5),
    ("AGG01", "Aggregate 3/4 inch", "Aggregates and sand", "Tonne", 5),
    ("AGG02", "Hardcore", "Aggregates and sand", "Trip", 1),
    ("BRK01", "Burnt bricks", "Bricks and blocks", "Piece", 1000),
    ("BLK01", "Concrete block 6 inch", "Bricks and blocks", "Piece", 500),
    ("STL01", "Steel bar Y12", "Steel and reinforcement", "Length", 50),
    ("STL02", "Steel bar Y16", "Steel and reinforcement", "Length", 50),
    ("STL03", "Steel bar Y20", "Steel and reinforcement", "Length", 30),
    ("STL04", "Binding wire", "Steel and reinforcement", "Kilogram", 20),
    ("TIM01", "Timber 2x4", "Timber", "Length", 50),
    ("TIM02", "Plywood 18 mm", "Timber", "Sheet", 20),
    ("ROF01", "Roofing sheet (iron sheet)", "Roofing", "Sheet", 20),
    ("ROF02", "Roofing nails", "Roofing", "Kilogram", 5),
    ("ELE01", "Electrical cable 2.5 mm", "Electrical", "Roll", 3),
    ("ELE02", "Conduit pipe", "Electrical", "Length", 50),
    ("PLB01", "PVC pipe 4 inch", "Plumbing and sanitary", "Length", 10),
    ("PLB02", "PVC pipe 1 inch", "Plumbing and sanitary", "Length", 20),
    ("PNT01", "Emulsion paint", "Paint and finishes", "Tin", 10),
    ("TIL01", "Floor tiles", "Tiles and flooring", "Square metre", 50),
    ("WTP01", "Waterproofing membrane", "Waterproofing", "Roll", 5),
    ("FUL01", "Diesel", "Fuel and lubricants", "Litre", 100),
    ("TLS01", "Nails (assorted)", "Tools and consumables", "Kilogram", 10),
    ("SAF01", "Safety helmet", "Safety equipment", "Piece", 10),
]

PHASES = [
    "Site clearing and setting out", "Excavation and foundation", "Ground floor slab", "Columns and beams",
    "Upper floors and slabs", "Walling and masonry", "Roofing", "Plumbing and drainage", "Electrical installation",
    "Plastering and finishes", "Tiling and flooring", "Doors, windows and joinery", "Painting",
    "External works and landscaping",
]


@transaction.atomic
def load_starter_data(project, user):
    """Add whatever of the starter list is missing. Returns how many of each were added."""
    require(user, "adjust_stock")  # Admin, Owner or Project Manager
    added = {"units": 0, "categories": 0, "materials": 0, "components": 0}
    units = {}
    for name, abbr in UNITS:
        unit, new = Unit.objects.get_or_create(name=name, defaults={"abbreviation": abbr})
        units[name] = unit
        added["units"] += new
    categories = {}
    for name in CATEGORIES:
        cat, new = MaterialCategory.objects.get_or_create(name=name)
        categories[name] = cat
        added["categories"] += new
    for code, name, cat, unit, reorder in MATERIALS:
        _, new = Material.objects.get_or_create(code=code, defaults={
            "name": name, "category": categories[cat], "unit": units[unit], "reorder_level": reorder,
            "created_by": user})
        added["materials"] += new
    for i, name in enumerate(PHASES, start=1):
        _, new = Phase.objects.get_or_create(project=project, name=name, defaults={
            "sequence": i, "kind": Phase.Kind.CONSTRUCTION})
        added["components"] += new
    if not any(added.values()):
        raise WorkflowError("Everything in the starter list is already there.")
    project._starter_added = added
    return project
