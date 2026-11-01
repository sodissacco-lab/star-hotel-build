from django.urls import path

from documents import views

urlpatterns = [
    path("", views.gallery, name="doc_gallery"),
    path("upload/", views.upload, name="doc_upload"),
    path("<int:pk>/", views.serve, name="doc_serve"),
]
