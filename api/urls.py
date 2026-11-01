from django.urls import path

from api import views

urlpatterns = [
    path("auth/login/", views.LoginView.as_view()),
    path("auth/logout/", views.LogoutView.as_view()),
    path("me/", views.MeView.as_view()),
    path("projects/", views.ProjectListView.as_view()),
    path("materials/", views.MaterialListView.as_view()),
    path("projects/<int:pk>/stock/", views.StockView.as_view()),
    path("projects/<int:pk>/boq/", views.BOQView.as_view()),
    path("projects/<int:pk>/diary/", views.DiaryListCreateView.as_view()),
    path("projects/<int:pk>/workers/", views.WorkerListView.as_view()),
    path("projects/<int:pk>/attendance/", views.AttendanceView.as_view()),
    path("projects/<int:pk>/requisitions/", views.RequisitionListCreateView.as_view()),
    path("projects/<int:pk>/suppliers/", views.SupplierSummaryView.as_view()),
    path("projects/<int:pk>/purchase-orders/<int:po_id>/account/", views.PurchaseOrderAccountView.as_view()),
    path("requisitions/<int:pk>/<str:action>/", views.RequisitionActionView.as_view()),
]