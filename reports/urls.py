from django.urls import path

from reports import views

urlpatterns = [
    path("", views.index, name="reports_index"),
    path("progress/", views.progress, name="report_progress"),
    path("materials/", views.materials, name="report_materials"),
    path("budget/", views.budget, name="report_budget"),
    path("payroll/", views.payroll, name="report_payroll"),
]
