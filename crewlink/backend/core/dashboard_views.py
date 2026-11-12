"""Read-only endpoints the leadership dashboard needs. Both are leader-only and scoped
to the caller's local by the server; nothing in the request chooses the local."""
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Announcement, AnnouncementRecipient, Member
from .permissions import IsLeader


class AudienceListView(APIView):
    """GET /api/announcements/audiences/ -- the caller's local and the classifications of
    its active members (with head counts), for the audience picker."""

    permission_classes = [IsLeader]

    def get(self, request):
        rows = (
            Member.objects.for_account(request.user)
            .filter(status="active")
            .values("classification")
            .annotate(members=Count("id"))
            .order_by("classification")
        )
        local = request.user.local
        return Response({"local": {"id": local.id, "name": local.name}, "classifications": list(rows)})


class AnnouncementProgressView(APIView):
    """GET /api/announcements/<id>/progress/ -- polled by the dashboard. `pushed` means the
    (simulated) provider accepted the push for that recipient; it is not proof the phone
    received it. `read` and `acknowledged` come from the member's own actions."""

    permission_classes = [IsLeader]

    def get(self, request, pk):
        announcement = get_object_or_404(Announcement.objects.for_account(request.user), pk=pk)
        counts = AnnouncementRecipient.objects.filter(announcement=announcement).aggregate(
            recipients=Count("id"),
            pushed=Count("id", filter=Q(sent_at__isnull=False)),
            read=Count("id", filter=Q(read_at__isnull=False)),
            acknowledged=Count("id", filter=Q(acked_at__isnull=False)),
        )
        return Response({
            "id": announcement.id,
            "status": announcement.status,
            "sent_at": announcement.sent_at,
            "needs_ack": announcement.needs_ack,
            "counts": counts,
        })
