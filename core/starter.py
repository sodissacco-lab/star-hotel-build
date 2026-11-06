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
    "Ironmongery", "External works", "Hotel services and equipment",
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
    # water tanks
    ("PLB10", "Water tank 500 litres", "Plumbing and sanitary", "Piece", 0),
    ("PLB11", "Water tank 1,000 litres", "Plumbing and sanitary", "Piece", 0),
    ("PLB12", "Water tank 2,000 litres", "Plumbing and sanitary", "Piece", 0),
    ("PLB13", "Water tank 3,000 litres", "Plumbing and sanitary", "Piece", 0),
    ("PLB14", "Water tank 10,000 litres", "Plumbing and sanitary", "Piece", 0),
    ("PLB15", "Water tank 20,000 litres", "Plumbing and sanitary", "Piece", 0),
    ("PLB16", "Tank stand (steel)", "Plumbing and sanitary", "Piece", 0),
    ("PLB17", "PVC pipe 1/2 inch", "Plumbing and sanitary", "Length", 20),
    ("PLB18", "PVC pipe 3/4 inch", "Plumbing and sanitary", "Length", 20),
    ("PLB19", "PVC pipe 6 inch", "Plumbing and sanitary", "Length", 5),
    ("PLB20", "PPR pipe 1/2 inch (hot and cold)", "Plumbing and sanitary", "Length", 20),
    ("PLB21", "PPR pipe 3/4 inch (hot and cold)", "Plumbing and sanitary", "Length", 20),
    ("PLB22", "Gate valve", "Plumbing and sanitary", "Piece", 5),
    ("PLB23", "Float valve", "Plumbing and sanitary", "Piece", 5),
    ("PLB24", "Pipe glue / solvent cement", "Plumbing and sanitary", "Tin", 5),
    ("PLB25", "Teflon tape", "Plumbing and sanitary", "Roll", 10),
    ("PLB26", "Manhole cover", "Plumbing and sanitary", "Piece", 0),
    ("PLB27", "Solar water heater", "Plumbing and sanitary", "Unit", 0),
    ("SAN05", "Urinal", "Plumbing and sanitary", "Piece", 0),
    ("SAN06", "Kitchen sink", "Plumbing and sanitary", "Piece", 0),
    ("SAN07", "Bathtub", "Plumbing and sanitary", "Piece", 0),
    ("SAN08", "Towel rail and bathroom accessories", "Plumbing and sanitary", "Set", 0),
    # timber and boards
    ("TIM03", "Timber 2x2", "Timber", "Length", 50),
    ("TIM04", "Timber 2x3", "Timber", "Length", 50),
    ("TIM05", "Timber 2x6", "Timber", "Length", 30),
    ("TIM06", "Timber 2x8", "Timber", "Length", 20),
    ("TIM07", "Timber 2x10", "Timber", "Length", 20),
    ("TIM08", "Timber 2x12", "Timber", "Length", 10),
    ("TIM09", "Timber 1x2", "Timber", "Length", 50),
    ("TIM10", "Timber 1x4", "Timber", "Length", 30),
    ("TIM11", "Timber 1x6", "Timber", "Length", 30),
    ("TIM12", "Timber 1x8 (fascia)", "Timber", "Length", 20),
    ("TIM13", "Timber 1x12", "Timber", "Length", 10),
    ("TIM14", "Timber 3x2", "Timber", "Length", 30),
    ("TIM15", "Timber 3x3", "Timber", "Length", 20),
    ("TIM16", "Timber 4x2", "Timber", "Length", 30),
    ("TIM17", "Timber 4x4", "Timber", "Length", 20),
    ("TIM18", "Timber 6x2", "Timber", "Length", 20),
    ("TIM19", "Timber 6x3", "Timber", "Length", 10),
    ("TIM20", "Plywood 12 mm", "Timber", "Sheet", 20),
    ("TIM21", "Plywood 9 mm", "Timber", "Sheet", 20),
    ("TIM22", "Plywood 6 mm", "Timber", "Sheet", 20),
    ("TIM23", "Blockboard 18 mm", "Timber", "Sheet", 10),
    ("TIM24", "MDF board 18 mm", "Timber", "Sheet", 10),
    ("TIM25", "Hardboard", "Timber", "Sheet", 10),
    ("TIM26", "Eucalyptus poles", "Timber", "Piece", 50),
    ("TIM27", "Hardwood for doors and frames", "Timber", "Length", 20),
    ("TIM28", "Timber preservative (creosote)", "Timber", "Litre", 20),
    # steel and ironmongery
    ("STL05", "Steel bar Y8", "Steel and reinforcement", "Length", 50),
    ("STL06", "Steel bar Y10", "Steel and reinforcement", "Length", 50),
    ("STL07", "Steel bar Y25", "Steel and reinforcement", "Length", 20),
    ("STL08", "Round bar R6 (stirrups)", "Steel and reinforcement", "Length", 50),
    ("STL09", "Hoop iron (brickforce)", "Steel and reinforcement", "Roll", 5),
    ("STL10", "Angle iron 40x40", "Steel and reinforcement", "Length", 10),
    ("STL11", "Angle iron 50x50", "Steel and reinforcement", "Length", 10),
    ("STL12", "Box tube 1x1 inch", "Steel and reinforcement", "Length", 10),
    ("STL13", "Box tube 2x2 inch", "Steel and reinforcement", "Length", 10),
    ("STL14", "Box tube 3x2 inch", "Steel and reinforcement", "Length", 10),
    ("STL15", "Flat bar", "Steel and reinforcement", "Length", 10),
    ("STL16", "Steel plate", "Steel and reinforcement", "Sheet", 0),
    ("STL17", "Welding rods", "Steel and reinforcement", "Kilogram", 10),
    ("STL18", "Chicken wire", "Steel and reinforcement", "Roll", 5),
    ("STL19", "Barbed wire", "Steel and reinforcement", "Roll", 5),
    ("STL20", "Concrete spacers", "Steel and reinforcement", "Bundle", 10),
    ("TLS04", "Wire nails 2 inch", "Tools and consumables", "Kilogram", 10),
    ("TLS05", "Wire nails 3 inch", "Tools and consumables", "Kilogram", 10),
    ("TLS06", "Wire nails 4 inch", "Tools and consumables", "Kilogram", 10),
    ("TLS07", "Wire nails 5 inch", "Tools and consumables", "Kilogram", 10),
    ("TLS08", "Wire nails 6 inch", "Tools and consumables", "Kilogram", 10),
    ("TLS09", "Concrete nails", "Tools and consumables", "Kilogram", 5),
    ("TLS10", "Bolts, nuts and washers", "Ironmongery", "Kilogram", 5),
    ("TLS11", "Wall plugs and anchor bolts", "Ironmongery", "Piece", 100),
    ("TLS12", "Hinges", "Ironmongery", "Piece", 20),
    ("TLS13", "Door locks and handles", "Ironmongery", "Set", 10),
    ("TLS14", "Padlocks", "Ironmongery", "Piece", 10),
    ("TLS15", "Bolts and latches", "Ironmongery", "Piece", 20),
    ("TLS16", "Polythene sheeting (curing and DPM)", "Waterproofing", "Roll", 5),
    ("TLS17", "Sandpaper", "Tools and consumables", "Piece", 20),
    ("TLS18", "Brushes and rollers", "Tools and consumables", "Piece", 20),
    ("TLS19", "Wheelbarrow", "Tools and consumables", "Piece", 0),
    ("TLS20", "Spades, hoes and shovels", "Tools and consumables", "Piece", 5),
    ("TLS21", "Hand tools (hammers, trowels, saws)", "Tools and consumables", "Set", 0),
    ("TLS22", "String line and pegs", "Tools and consumables", "Roll", 5),
    # cement, walling and plaster
    ("CEM02", "White cement", "Cement and binders", "Bag", 5),
    ("CEM03", "Tile adhesive", "Cement and binders", "Bag", 20),
    ("CEM04", "Tile grout", "Cement and binders", "Kilogram", 20),
    ("CEM05", "Wall putty / skimming", "Cement and binders", "Bag", 20),
    ("CEM06", "Ready-mix concrete", "Cement and binders", "Cubic metre", 0),
    ("BLK02", "Concrete block 4 inch", "Bricks and blocks", "Piece", 500),
    ("BLK03", "Concrete block 9 inch", "Bricks and blocks", "Piece", 500),
    ("BLK04", "Hollow block 6 inch", "Bricks and blocks", "Piece", 500),
    ("BRK02", "Face bricks", "Bricks and blocks", "Piece", 1000),
    ("AGG03", "Aggregate 1/2 inch", "Aggregates and sand", "Tonne", 5),
    ("AGG04", "Quarry dust", "Aggregates and sand", "Tonne", 5),
    # roofing
    ("ROF03", "Roofing sheet, gauge 28 (long span)", "Roofing", "Metre", 50),
    ("ROF04", "Roofing sheet, gauge 30", "Roofing", "Sheet", 20),
    ("ROF05", "Roofing screws", "Roofing", "Kilogram", 5),
    ("ROF06", "Wall plate and roof fixings", "Roofing", "Kilogram", 5),
    ("ROF07", "Sisalation / roof underlay", "Roofing", "Roll", 3),
    ("ROF08", "Fascia and barge boards", "Roofing", "Length", 10),
    ("ROF09", "Roof insulation", "Roofing", "Square metre", 50),
    # doors and windows
    ("DWR01", "Flush door with frame", "Doors and windows", "Piece", 0),
    ("DWR02", "Hotel room door (solid)", "Doors and windows", "Piece", 0),
    ("DWR03", "Steel security door", "Doors and windows", "Piece", 0),
    ("DWR04", "Aluminium window with glass", "Doors and windows", "Square metre", 0),
    ("DWR05", "Aluminium sliding door", "Doors and windows", "Piece", 0),
    ("DWR06", "Glass 6 mm", "Doors and windows", "Square metre", 0),
    ("DWR07", "Burglar proof bars", "Doors and windows", "Square metre", 0),
    ("DWR08", "Door frames", "Doors and windows", "Piece", 0),
    ("DWR09", "Curtain rails and fittings", "Doors and windows", "Set", 0),
    # ceiling and partitions
    ("CLG01", "Gypsum board 12.5 mm", "Ceiling and partitions", "Sheet", 20),
    ("CLG02", "Ceiling channels and hangers", "Ceiling and partitions", "Length", 30),
    ("CLG03", "PVC ceiling panels", "Ceiling and partitions", "Square metre", 50),
    ("CLG04", "Cornice", "Ceiling and partitions", "Length", 30),
    ("CLG05", "Partition boards", "Ceiling and partitions", "Sheet", 10),
    ("CLG06", "Gypsum jointing compound and tape", "Ceiling and partitions", "Set", 5),
    # tiles, paint and finishes
    ("TIL02", "Wall tiles", "Tiles and flooring", "Square metre", 50),
    ("TIL03", "Granite and marble", "Tiles and flooring", "Square metre", 0),
    ("TIL04", "Skirting tiles", "Tiles and flooring", "Metre", 50),
    ("TIL05", "Tile spacers", "Tiles and flooring", "Bundle", 10),
    ("TIL06", "Carpet", "Tiles and flooring", "Square metre", 0),
    ("TIL07", "Vinyl flooring", "Tiles and flooring", "Square metre", 0),
    ("TIL08", "Outdoor / anti-slip tiles", "Tiles and flooring", "Square metre", 50),
    ("PNT02", "Gloss / enamel paint", "Paint and finishes", "Tin", 10),
    ("PNT03", "Undercoat / primer", "Paint and finishes", "Tin", 10),
    ("PNT04", "Exterior weather paint", "Paint and finishes", "Tin", 10),
    ("PNT05", "Wood varnish", "Paint and finishes", "Tin", 5),
    ("PNT06", "Thinners", "Paint and finishes", "Litre", 10),
    ("PNT07", "Wall sealer", "Paint and finishes", "Tin", 5),
    # external works
    ("EXT01", "Paving blocks", "External works", "Square metre", 100),
    ("EXT02", "Kerbstones", "External works", "Piece", 50),
    ("EXT03", "Perimeter fence posts", "External works", "Piece", 20),
    ("EXT04", "Gate (steel)", "External works", "Piece", 0),
    ("EXT05", "Topsoil", "External works", "Trip", 0),
    ("EXT06", "Grass and landscaping plants", "External works", "Square metre", 0),
    ("EXT07", "Drainage channels", "External works", "Metre", 20),
    ("EXT08", "Culvert pipes", "External works", "Piece", 0),
    ("EXT09", "Murram for access roads", "External works", "Trip", 0),
    ("EXT10", "Outdoor lighting", "External works", "Piece", 0),
    # electrical, extra
    ("ELE11", "Electrical cable 10 mm", "Electrical", "Roll", 2),
    ("ELE12", "Armoured cable", "Electrical", "Metre", 50),
    ("ELE13", "Conduit fittings and junction boxes", "Electrical", "Piece", 50),
    ("ELE14", "Circuit breakers (MCB)", "Electrical", "Piece", 20),
    ("ELE15", "Electric meter", "Electrical", "Unit", 0),
    ("ELE16", "Solar panels and inverter", "Electrical", "Set", 0),
    ("ELE17", "LED bulbs and tubes", "Electrical", "Piece", 50),
    ("ELE18", "Data and TV cable", "Electrical", "Roll", 3),
    ("ELE19", "Insulation tape", "Electrical", "Roll", 20),
    # hotel services
    ("HSV01", "Air conditioning unit", "Hotel services and equipment", "Unit", 0),
    ("HSV02", "Ceiling fans", "Hotel services and equipment", "Piece", 0),
    ("HSV03", "Fire extinguisher", "Hotel services and equipment", "Piece", 0),
    ("HSV04", "Smoke detectors and fire alarm", "Hotel services and equipment", "Set", 0),
    ("HSV05", "CCTV camera set", "Hotel services and equipment", "Set", 0),
    ("HSV06", "Lift", "Hotel services and equipment", "Unit", 0),
    ("HSV07", "Kitchen equipment", "Hotel services and equipment", "Set", 0),
    ("HSV08", "Intercom / room phone system", "Hotel services and equipment", "Set", 0),
    ("HSV09", "Wi-Fi and network equipment", "Hotel services and equipment", "Set", 0),
    ("HSV10", "Water treatment / filtration plant", "Hotel services and equipment", "Unit", 0),
    ("HSV11", "Hot water boiler / heat pump", "Hotel services and equipment", "Unit", 0),
    ("HSV12", "Ventilation and extractor fans", "Hotel services and equipment", "Piece", 0),
    # safety, extra
    ("SAF05", "Dust masks", "Safety equipment", "Piece", 20),
    ("SAF06", "Safety goggles", "Safety equipment", "Piece", 10),
    ("SAF07", "Safety harness", "Safety equipment", "Piece", 0),
    ("SAF08", "First aid kit", "Safety equipment", "Piece", 2),
    ("SAF09", "Caution tape and signs", "Safety equipment", "Roll", 5),
    ("SAF10", "Overalls", "Safety equipment", "Piece", 10),
    ("SAF11", "Ear protection", "Safety equipment", "Piece", 10),
    ("FUL04", "Hydraulic oil", "Fuel and lubricants", "Litre", 10),
    ("FUL05", "Grease", "Fuel and lubricants", "Kilogram", 5),
    # provision for items not on the list: pick 'Other' and say what it is in the Notes box
    ("OTH01", "Other - Cement and binders (specify in notes)", "Cement and binders", "Piece", 0),
    ("OTH02", "Other - Aggregates and sand (specify in notes)", "Aggregates and sand", "Piece", 0),
    ("OTH03", "Other - Bricks and blocks (specify in notes)", "Bricks and blocks", "Piece", 0),
    ("OTH04", "Other - Steel and reinforcement (specify in notes)", "Steel and reinforcement", "Piece", 0),
    ("OTH05", "Other - Timber (specify in notes)", "Timber", "Piece", 0),
    ("OTH06", "Other - Roofing (specify in notes)", "Roofing", "Piece", 0),
    ("OTH07", "Other - Doors and windows (specify in notes)", "Doors and windows", "Piece", 0),
    ("OTH08", "Other - Electrical (specify in notes)", "Electrical", "Piece", 0),
    ("OTH09", "Other - Plumbing and sanitary (specify in notes)", "Plumbing and sanitary", "Piece", 0),
    ("OTH10", "Other - Paint and finishes (specify in notes)", "Paint and finishes", "Piece", 0),
    ("OTH11", "Other - Tiles and flooring (specify in notes)", "Tiles and flooring", "Piece", 0),
    ("OTH12", "Other - Ceiling and partitions (specify in notes)", "Ceiling and partitions", "Piece", 0),
    ("OTH13", "Other - Waterproofing (specify in notes)", "Waterproofing", "Piece", 0),
    ("OTH14", "Other - Fuel and lubricants (specify in notes)", "Fuel and lubricants", "Piece", 0),
    ("OTH15", "Other - Tools and consumables (specify in notes)", "Tools and consumables", "Piece", 0),
    ("OTH16", "Other - Safety equipment (specify in notes)", "Safety equipment", "Piece", 0),
    ("OTH17", "Other - Formwork and scaffolding (specify in notes)", "Formwork and scaffolding", "Piece", 0),
    ("OTH18", "Other - Ironmongery (specify in notes)", "Ironmongery", "Piece", 0),
    ("OTH19", "Other - External works (specify in notes)", "External works", "Piece", 0),
    ("OTH20", "Other - Hotel services and equipment (specify in notes)", "Hotel services and equipment", "Piece", 0),
]

PHASES = [
    "Site clearing and setting out", "Excavation and foundation", "Ground floor slab", "Columns and beams",
    "Upper floors and slabs", "Walling and masonry", "Roofing", "Plumbing and drainage", "Electrical installation",
    "Plastering and finishes", "Tiling and flooring", "Doors, windows and joinery", "Painting",
    "External works and landscaping", "Ceiling and partitions", "Hotel services and fit-out",
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
    have_names = {n.lower() for n in Material.objects.values_list("name", flat=True)}
    for code, name, cat, unit, reorder in MATERIALS:
        if name.lower() in have_names and not Material.objects.filter(code=code).exists():
            continue  # you already added this one yourself under another code
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
