from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from boq import views as boq_views
from core import views as core_views
from finance import views as finance_views
from inventory import views as inventory_views
from workforce import views as workforce_views

admin.site.site_header = "StarBuild Administration"
admin.site.site_title = "StarBuild"
admin.site.index_title = "Hotel Construction Management System"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("api.urls")),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", core_views.dashboard, name="dashboard"),
    path("stock/", inventory_views.stock_report, name="stock_report"),
    path("budget/", finance_views.budget_report, name="budget_report"),
    path("boq/", boq_views.boq_report, name="boq_report"),
    path("suppliers/", finance_views.supplier_list, name="supplier_list"),
    path("suppliers/<int:pk>/", finance_views.supplier_statement, name="supplier_statement"),
    path("receipts/", finance_views.receipt_list, name="receipt_list"),
    path("receipts/print/", finance_views.receipt_batch, name="receipt_batch"),
    path("receipts/<int:pk>/", finance_views.receipt_print, name="receipt_print"),
    path("files/", include("documents.urls")),
    path("site/", include("fieldwork.urls")),
    path("labour/", workforce_views.labour_report, name="labour_report"),
]