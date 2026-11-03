from django.urls import path

from api import field_views, views
from notifications import views as notification_views

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
    path("projects/<int:pk>/issues/", field_views.IssueView.as_view()),
    path("projects/<int:pk>/receivable-orders/", field_views.ReceivableOrdersView.as_view()),
    path("projects/<int:pk>/purchase-orders/<int:po_id>/receive/", field_views.ReceiveGoodsView.as_view()),
    path("projects/<int:pk>/direct-deliveries/", field_views.DirectDeliveryView.as_view()),
    path("suppliers/", field_views.SupplierListView.as_view()),
    path("last-prices/", field_views.LastPricesView.as_view()),
    path("projects/<int:pk>/files/", field_views.FileListCreateView.as_view()),
    path("files/<int:pk>/download/", field_views.FileDownloadView.as_view()),
    path("notifications/", notification_views.NotificationListView.as_view()),
    path("notifications/read/", notification_views.NotificationReadView.as_view()),
    path("approvals/", field_views.ApprovalListView.as_view()),
    path("approvals/<str:kind>/<int:pk>/<str:action>/", field_views.ApprovalActionView.as_view()),
    path("requisitions/<int:pk>/<str:action>/", views.RequisitionActionView.as_view()),
]