from django.urls import path

from . import dashboard_views, views

urlpatterns = [
    path("health/", views.health, name="health"),
    path("auth/login/", views.LoginView.as_view(), name="login"),
    path("me/", views.MeView.as_view(), name="me"),
    path("me/inbox/", views.InboxListView.as_view(), name="inbox"),
    path("me/inbox/<uuid:pk>/read/", views.InboxReadView.as_view(), name="inbox-read"),
    path("me/inbox/<uuid:pk>/ack/", views.InboxAckView.as_view(), name="inbox-ack"),
    path("announcements/", views.AnnouncementListCreateView.as_view(), name="announcement-list"),
    path("announcements/draft/", views.AnnouncementDraftView.as_view(), name="announcement-draft"),
    path("announcements/audiences/", dashboard_views.AudienceListView.as_view(), name="announcement-audiences"),
    path("announcements/<uuid:pk>/approve/", views.AnnouncementApproveView.as_view(), name="announcement-approve"),
    path("announcements/<uuid:pk>/progress/", dashboard_views.AnnouncementProgressView.as_view(), name="announcement-progress"),
    path("announcements/<uuid:pk>/", views.AnnouncementDetailView.as_view(), name="announcement-detail"),
    path("announcements/<uuid:pk>/send/", views.AnnouncementSendView.as_view(), name="announcement-send"),
]
