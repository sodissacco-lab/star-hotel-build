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
    ("Length", "len"), ("Trip", "trip"), ("Set", "set"), ("Unit", "unit"),
]

CATEGORIES = [
    "Cement and binders", "Aggregates and sand", "Bricks and blocks", "Steel and reinforcement", "Timber", "Roofing",
    "Doors and windows", "Electrical", "Plumbing and sanitary", "Paint and finishes", "Tiles and flooring",
    "Ceiling and partitions", "Waterproofing", "Fuel and lubricants", "Tools and consumables", "Safety equipment", "Formwork and scaffolding",
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
    # structure and masonry
    ("DPC01", "Damp-proof course", "Waterproofing", "Roll", 3),
    ("BRC01", "Mesh reinforcement BRC", "Steel and reinforcement", "Sheet", 10),
    ("FRM01", "Formwork timber", "Formwork and scaffolding", "Length", 50),
    ("SCF01", "Props and scaffolding", "Formwork and scaffolding", "Set", 0),
    ("ADM01", "Concrete admixture", "Cement and binders", "Litre", 20),
    ("LIM01", "Lime", "Cement and binders", "Bag", 10),
    ("MUR01", "Murram", "Aggregates and sand", "Trip", 1),
    ("RUB01", "Rubble stone", "Aggregates and sand", "Trip", 1),
    ("PLS01", "Plaster sand", "Aggregates and sand", "Tonne", 5),
    ("WAT01", "Water", "Tools and consumables", "Trip", 0),
    # roofing
    ("TRS01", "Roof trusses", "Roofing", "Piece", 0),
    ("PRL01", "Purlins, steel", "Roofing", "Length", 20),
    ("RDG01", "Ridge caps", "Roofing", "Piece", 10),
    ("GUT01", "Gutters and downpipes", "Roofing", "Length", 10),
    # electrical
    ("ELE03", "Electrical cable 1.5 mm", "Electrical", "Roll", 3),
    ("ELE04", "Electrical cable 4 mm", "Electrical", "Roll", 3),
    ("ELE05", "Electrical cable 6 mm", "Electrical", "Roll", 2),
    ("ELE06", "Switches and sockets", "Electrical", "Piece", 30),
    ("ELE07", "Distribution board", "Electrical", "Piece", 0),
    ("ELE08", "Light fittings", "Electrical", "Piece", 20),
    ("ELE09", "Earthing rod", "Electrical", "Piece", 5),
    ("ELE10", "Backup generator", "Electrical", "Unit", 0),
    # plumbing and sanitary
    ("PLB03", "PVC pipe 2 inch", "Plumbing and sanitary", "Length", 10),
    ("PLB04", "PVC pipe 3 inch", "Plumbing and sanitary", "Length", 10),
    ("PLB05", "Pipe fittings (elbows, tees)", "Plumbing and sanitary", "Piece", 50),
    ("PLB06", "Water tank 5,000 litres", "Plumbing and sanitary", "Piece", 0),
    ("PLB07", "Water pump", "Plumbing and sanitary", "Piece", 0),
    ("PLB08", "Floor drains", "Plumbing and sanitary", "Piece", 10),
    ("PLB09", "Septic tank rings", "Plumbing and sanitary", "Piece", 0),
    ("SAN01", "Toilet (WC)", "Plumbing and sanitary", "Set", 0),
    ("SAN02", "Hand basin", "Plumbing and sanitary", "Piece", 0),
    ("SAN03", "Shower set", "Plumbing and sanitary", "Set", 0),
    ("SAN04", "Taps and mixers", "Plumbing and sanitary", "Piece", 10),
    # finishes and waterproofing
    ("WTP02", "Bituminous paint", "Waterproofing", "Tin", 5),
    # fuel and site
    ("FUL02", "Petrol", "Fuel and lubricants", "Litre", 50),
    ("FUL03", "Engine oil", "Fuel and lubricants", "Litre", 10),
    ("TLS02", "Screws and fixings", "Tools and consumables", "Kilogram", 5),
    ("TLS03", "Cutting discs", "Tools and consumables", "Piece", 20),
    ("SAF02", "Work gloves", "Safety equipment", "Piece", 20),
    ("SAF03", "Safety boots", "Safety equipment", "Piece", 10),
    ("SAF04", "Reflective jackets", "Safety equipment", "Piece", 10),
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