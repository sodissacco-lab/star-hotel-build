from django.urls import path

from fieldwork import views

urlpatterns = [
    path("", views.home, name="fw_home"),
    path("diary/", views.diary, name="fw_diary"),
    path("attendance/", views.attendance, name="fw_attendance"),
    path("requisition/", views.requisition, name="fw_requisition"),
    path("issue/", views.issue, name="fw_issue"),
    path("receive/", views.receive_pick, name="fw_receive_pick"),
    path("receive/<int:po_id>/", views.receive, name="fw_receive"),
]
