from django.contrib.auth import authenticate
from django.db import connection
from django.db.models import Count, DateTimeField, F, Q, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.exceptions import APIException
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Announcement, AnnouncementRecipient
from .permissions import IsAccount, IsLeader
from .sending import send_announcement
from .serializers import AnnouncementSerializer, InboxItemSerializer, LoginSerializer


class Conflict(APIException):
    status_code = 409
    default_detail = "Conflict."


# --------------------------------------------------------------------- public
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])  # public by design; also listed in core.checks.PUBLIC_ROUTES
def health(request):
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception:
        return Response({"status": "error", "database": "unreachable"}, status=503)
    return Response({"status": "ok", "database": "ok"})


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]  # public by design; also listed in core.checks.PUBLIC_ROUTES

    def post(self, request):
        data = LoginSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        account = authenticate(
            request, username=data.validated_data["username"], password=data.validated_data["password"]
        )
        if account is None:  # same answer for unknown user and wrong password
            return Response({"detail": "Invalid credentials."}, status=401)
        token, _ = Token.objects.get_or_create(user=account)
        return Response({
            "token": token.key,
            "role": account.role,
            "local": {"id": account.local_id, "name": account.local.name},
        })


# --------------------------------------------------------- any logged-in account
class MeView(APIView):
    """The caller's own identity. Takes no id, so there is nothing to tamper with."""

    permission_classes = [IsAccount]

    def get(self, request):
        account = request.user
        member = account.member
        return Response({
            "username": account.username,
            "role": account.role,
            "local": {"id": account.local_id, "name": account.local.name},
            "member": {
                "id": member.id, "full_name": member.full_name,
                "email": member.email, "classification": member.classification,
            },
        })


# ------------------------------------------------------------ leadership only
class AnnouncementScopedMixin:
    def get_queryset(self):
        # the local comes from the authenticated account, never from the request
        return Announcement.objects.for_account(self.request.user).order_by("-created_at")


class AnnouncementListCreateView(AnnouncementScopedMixin, generics.ListCreateAPIView):
    permission_classes = [IsLeader]
    serializer_class = AnnouncementSerializer

    def perform_create(self, serializer):
        user = self.request.user
        serializer.save(local_id=user.local_id, created_by=user, status=Announcement.Status.DRAFT)


class AnnouncementDetailView(AnnouncementScopedMixin, generics.RetrieveUpdateAPIView):
    permission_classes = [IsLeader]
    serializer_class = AnnouncementSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def perform_update(self, serializer):
        if serializer.instance.status != Announcement.Status.DRAFT:
            raise Conflict("Only drafts can be edited.")
        serializer.save()


class AnnouncementSendView(APIView):
    """POST /api/announcements/<id>/send/

    First call that wins the conditional UPDATE -> 202 and the recipients are created.
    Any repeat or concurrent loser -> 200 with already_sent=true and nothing is created.
    Not approved -> 409. Another local's announcement -> 404 (same as nonexistent).
    """

    permission_classes = [IsLeader]

    def post(self, request, pk):
        announcement = get_object_or_404(Announcement.objects.for_account(request.user), pk=pk)
        if announcement.status == Announcement.Status.DRAFT:
            raise Conflict("Announcement must be approved before it can be sent.")

        won, created = send_announcement(announcement)
        announcement.refresh_from_db()

        counts = AnnouncementRecipient.objects.filter(announcement=announcement).aggregate(
            recipients=Count("id"),
            pushed=Count("id", filter=Q(sent_at__isnull=False)),
            read=Count("id", filter=Q(read_at__isnull=False)),
            acknowledged=Count("id", filter=Q(acked_at__isnull=False)),
        )
        return Response(
            {
                "id": announcement.id,
                "status": announcement.status,
                "sent_at": announcement.sent_at,
                "already_sent": not won,
                "recipients_created": created,
                "counts": counts,
            },
            status=202 if won else 200,
        )


# ------------------------------------------------------------- member inbox
# Every query below is pinned to the caller's own member row. The URL carries only an
# announcement id, never a recipient id or a member id, so there is no id a caller can
# change to reach somebody else's row: an announcement that was not addressed to the
# caller looks exactly like one that does not exist (404).
def own_recipients(account):
    return (
        AnnouncementRecipient.objects.for_account(account)
        .filter(member_id=account.member_id)
        .select_related("announcement")
    )


class InboxListView(generics.ListAPIView):
    """GET /api/me/inbox/ -- the durable inbox. Items exist from the moment the send
    commits, whether or not the push was ever accepted, so a member who was offline
    gets everything on reconnect. Contains no other member's data."""

    permission_classes = [IsAccount]
    serializer_class = InboxItemSerializer

    def get_queryset(self):
        return own_recipients(self.request.user).order_by("-announcement__sent_at", "-id")


class InboxItemActionView(APIView):
    """Base for read/ack. POST is idempotent: the first call stamps the timestamp, every
    repeat returns the same item unchanged (the UPDATE only matches rows still NULL, so
    concurrent duplicates cannot overwrite each other)."""

    permission_classes = [IsAccount]

    def post(self, request, pk):
        item = get_object_or_404(own_recipients(request.user), announcement_id=pk)
        self.apply(item)
        item.refresh_from_db()
        return Response(InboxItemSerializer(item).data)

    def apply(self, item):
        raise NotImplementedError


class InboxReadView(InboxItemActionView):
    """POST /api/me/inbox/<announcement_id>/read/"""

    def apply(self, item):
        AnnouncementRecipient.objects.filter(pk=item.pk, read_at__isnull=True).update(
            read_at=timezone.now()
        )


class InboxAckView(InboxItemActionView):
    """POST /api/me/inbox/<announcement_id>/ack/ -- only for announcements that asked
    for acknowledgement. Acknowledging implies having read it, so read_at is filled in
    if it was still empty (an existing read_at is kept)."""

    def apply(self, item):
        if not item.announcement.needs_ack:
            raise Conflict("This announcement does not request acknowledgement.")
        now = timezone.now()
        AnnouncementRecipient.objects.filter(pk=item.pk, acked_at__isnull=True).update(
            acked_at=now,
            read_at=Coalesce(F("read_at"), Value(now), output_field=DateTimeField()),
        )
