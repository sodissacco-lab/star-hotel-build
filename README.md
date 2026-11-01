# StarBuild – Hotel Construction Management System (HCMS)

Phase 1 foundation for the **Soroti Star Hotel** project (4-star with selected 5-star facilities).
Stack: Python, Django, PostgreSQL (SQLite for local development). Django REST Framework and the
Flutter app come in a later phase.

## What Phase 1 contains

| Area | What it does |
|---|---|
| Accounts | Custom user with the 12 roles from the blueprint; role-based capabilities (`accounts/permissions.py`) |
| Projects | Multi-project. Project particulars, 25 construction components + hotel facilities with budget and % complete, daily site diary with activities |
| Inventory | Materials register, suppliers, purchase orders, goods received, material issues, returns, stocktake adjustments, wastage |
| Stock ledger | Append-only ledger. Balances are always calculated (received + returned - issued - wastage +/- adjustments), never typed in. Valued at weighted-average cost |
| Finance | Budget lines, expenses (approval), payments with outstanding balances. Goods received automatically create an approved material expense |
| Controls | No negative stock, no over-receiving, purchase orders above the threshold need Owner/Director approval, creators cannot approve their own PO/expense/stocktake |
| Dashboard | Budget vs spend, outstanding payments, stock value, materials purchased/consumed, low-stock alerts, progress, recent diaries |

## First-time setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then set the values (or export them as environment variables)

# Migrations are not stored in the repository yet. Generate them once, then commit them:
python manage.py makemigrations accounts projects inventory finance boq procurement workforce
python manage.py migrate

python manage.py createsuperuser
python manage.py seed_soroti_star_hotel   # Soroti Star Hotel project + starter materials
python manage.py test                     # run the stock/procurement tests
python manage.py runserver
```

Then sign in at `/admin/`, set the superuser's role to *System Administrator*, create the other users
(Storekeeper, Project Manager, Accountant ...), and add each user to the project under
**Projects -> Soroti Star Hotel -> Members**.

## Daily workflow (all under "Records & actions")

1. **Purchase order**: create with lines -> action *Submit* -> a different authorised user runs *Approve*.
2. **Goods received**: storekeeper records the delivery (delivered / accepted / rejected) -> action *Post receipts to stock*.
   Stock increases and an approved material expense is created.
3. **Material issue**: storekeeper records what goes to site -> action *Post issues*. Blocked if stock is insufficient.
4. **Expenses / payments**: other costs are entered as pending, approved by someone else, then paid by the Accountant.
5. **Stocktake**: record the counted quantity -> an authorised user posts it; the difference is booked.
6. **Site diary**: one entry per day with several activities.

Posted documents are locked. Corrections are made with new documents (returns, adjustments), never by editing.

## Phase 2 (added)

| Area | What it does |
|---|---|
| BOQ (`boq`) | BOQ items with original and revised quantity, rate and cost. Work is recorded as measurements (cannot exceed the approved quantity). Quantity changes only through **variation orders**, approved by someone other than the author |
| Requisitions (`procurement`) | Site engineer raises -> **Submit** -> Quantity Surveyor **Verifies** (checked against remaining BOQ quantity and the materials budget) -> **Approve** -> supplier quotations -> **Create draft purchase order** (lowest or selected quotation). Emergency requisitions skip verification but need a justification |
| Budget report (`/budget/`) | Budget vs actual by category and component, flags for approaching / exceeded / unplanned spending, and what is owed to each supplier |
| BOQ report (`/boq/`) | Progress and earned value per item, BOQ estimate vs component budget, and a flag when materials issued to site exceed the BOQ quantity |

Requisition actions are in the admin list under **Requisitions** (actions menu, numbered 1-4 in order).
`MIN_QUOTATIONS` (default 1) sets how many quotations a normal requisition needs before an order can be raised.

## Phase 3 (added): labour and contractors (`workforce`)

| Area | What it does |
|---|---|
| Contractors and contracts | Contractor register (insurance expiry), contracts with amount, retention %, terms and guarantee |
| Payment certificates | Raised against an active contract. Approval (by someone else) books the **net amount** (gross less retention) as an approved subcontractor expense and tracks retention held. Total certified can never exceed the contract amount |
| Workers and attendance | Worker register (trade, direct hire or contractor's worker, daily wage). Daily attendance with hours and overtime, optionally per component |
| Payroll | Add a payroll (project + period) and it builds itself from **unpaid** attendance of direct-hire workers. Approval books an approved labour expense. Each attendance day can be paid only once; deleting a draft frees its days |
| Labour page (`/labour/`) | Headcount for 14 days, direct labour cost this month by trade and component, and per-contract certified / retention / paid / unpaid |

Pay rules: a full day = `STANDARD_WORK_HOURS` (8); fewer hours are paid pro rata; overtime is paid at
`daily wage / 8 x OVERTIME_MULTIPLIER` (1.5). Change both in settings or the environment. Contractor workers are
recorded for headcount but are paid through their contractor's certificates, not payroll.

## Not built yet (next phases, per the blueprint)

BOQ import from Excel/CSV, equipment, quality and HSE,
documents and photos, hotel room register, classification checklist, REST API and the Flutter mobile app.

## Deployment notes

Set `DJANGO_DEBUG=0`, a long `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, and the
`DB_*` PostgreSQL variables. Run `python manage.py collectstatic` and serve with
`gunicorn config.wsgi` behind Nginx with HTTPS. Take automated daily PostgreSQL backups.

## BOQ import (added)

In the admin open **BOQ items** and click **Import BOQ from Excel / CSV**. Required columns: `code, description, unit, quantity, rate`; optional: `phase` (exact component name in the project) and `material` (material code). Heading/sub-total rows (no quantity and no rate) are skipped. The file is checked as a whole; if any row is wrong nothing is saved and each problem is listed with its row number. Leave **Check only** ticked to test a file first. Existing item codes are rejected (quantities change only by variation order). Run `pip install -r requirements.txt` once (adds `openpyxl`).

## Mobile API (added)

Base URL `/api/v1/`, JSON, token authentication: send `Authorization: Token <token>`.
Run `pip install -r requirements.txt` then `python manage.py migrate` (creates the token table).

| Method and path | Purpose |
|---|---|
| `POST auth/login/` `{username,password}` | returns `token` and the user with `role` and `capabilities` (use these to show or hide buttons in the app) |
| `POST auth/logout/` | deletes the token |
| `GET me/`, `GET projects/`, `GET materials/` | profile, projects the user belongs to (with components), material list |
| `GET projects/<id>/stock/` | stock balances, value, reorder flags |
| `GET projects/<id>/boq/` | BOQ progress and earned value (needs `view_boq`) |
| `GET/POST projects/<id>/diary/` | site diaries with activities (POST needs `record_diary`) |
| `GET projects/<id>/workers/` | active workers (needs `record_attendance`) |
| `GET/POST projects/<id>/attendance/` | POST a whole day: `{date, entries:[{worker,status,hours_worked,overtime_hours,phase,notes}]}`; re-posting corrects it; all-or-nothing |
| `GET/POST projects/<id>/requisitions/` | list / raise a requisition with lines (needs `raise_requisition`) |
| `POST requisitions/<id>/submit｜verify｜approve｜reject/` | the same workflow and segregation-of-duties rules as the admin |

Projects you do not belong to return 404. Errors are `{"detail": "..."}` or field errors, with 400/401/403/404.

## Receipts (added)

Every payment recorded in **Payments** automatically issues a numbered receipt (RCT-00001...) to its payee: the **supplier** for purchases (goods received become approved expenses, so every purchase is covered), the **contractor** for payment certificates, or the expense description for other costs.
**Worker wages** are receipted per worker: pay the payroll's wages in full under Payments, then in **Payrolls** choose the action *Issue worker receipts*. Print the batch from **Receipts** (action *Print selected receipts*) or `/receipts/print/?payroll=<id>`.

* `/receipts/` is the register (search by name or number, filter by payee type and dates, totals exclude void receipts); each receipt prints with the amount in words and a signature line for the payee.
* Receipts cannot be edited or deleted. A wrong receipt is **voided** (open it in the admin and enter a void reason; Accountant, Project Manager or Admin) and a new one is issued. Void receipts stay in the register, so numbering has no gaps.
* Run `python manage.py makemigrations finance` then `migrate` after adding these files. Existing payments recorded before this feature have no receipt.

## Supplier accounts, advances and demand notes (added)

**How a supplier is paid.** In **Payments** choose the supplier's **purchase order** (not an expense). The system works out the rest:

* If the payment is for goods already delivered, it is recorded and receipted at once.
* If it is more than the value delivered, it is an **advance**: it waits for approval by the Owner or Project Manager (not the person who recorded it). Above `ADVANCE_MAX_PERCENT` (50%) of the order value only the Owner or Admin can approve. The receipt is issued on approval.
* When goods are received, advances already paid are applied to the delivery automatically (oldest first), so the expense, balance due and advance outstanding always agree.
* A payment can never take the total paid above the order value.

**Demand notes** (supplier invoices, demands, pro-formas): record under **Demand notes** -> *Verify* (checked against deliveries; a demand larger than delivered value is flagged **advance request**) -> *Approve for payment*. Payments are then linked to the demand note and its paid and balance update automatically.

**Where to see it**
* Purchase order screen: *Supplier account* line (delivered, paid, ADVANCE or BALANCE DUE).
* `/suppliers/`: all suppliers with ordered, delivered, paid, advance (goods not delivered) and balance due; `/suppliers/<id>/`: printable statement with the **register of items supplied**, payments and demand notes.
* Budget report: advances shown separately (not counted as spending until delivered). Dashboard: advances card, and a warning when an advance has had no full delivery for `ADVANCE_ALERT_DAYS` (30) days.
* Receipts to suppliers list the items (ordered, delivered, outstanding, prices) and the account position as at the day of payment, with a banner such as ADVANCE PAYMENT - goods not yet delivered. Contractor receipts show contract sum, certified, retention and balance; worker receipts show days, regular and overtime pay.
* Mobile API: `GET projects/<id>/suppliers/` and `GET projects/<id>/purchase-orders/<po>/account/`.

Run `python manage.py makemigrations finance` then `migrate`. Goods already received on existing orders are unaffected; payments recorded earlier against goods expenses stay as they are.