from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.health, name="health"),
    path("auth/login/", views.LoginView.as_view(), name="login"),
    path("me/", views.MeView.as_view(), name="me"),
    path("announcements/", views.AnnouncementListCreateView.as_view(), name="announcement-list"),
    path("announcements/<uuid:pk>/", views.AnnouncementDetailView.as_view(), name="announcement-detail"),
    path("announcements/<uuid:pk>/send/", views.AnnouncementSendView.as_view(), name="announcement-send"),
]
