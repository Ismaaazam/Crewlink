from django.contrib.auth import authenticate
from django.db import connection, transaction
from django.db.models import Count, DateTimeField, F, Q, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import ai
from .models import Announcement, AnnouncementRecipient
from .permissions import IsAccount, IsLeader
from .sending import send_announcement
from .serializers import (
    AnnouncementSerializer, DraftRequestSerializer, InboxItemSerializer, LoginSerializer,
)


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

    def partial_update(self, request, *args, **kwargs):
        """Any edit sends the announcement back to draft and clears the approval, so what
        was approved is always exactly what gets sent. The row is locked for the check
        and the write, so an edit cannot race a send: whichever commits first wins and
        the other gets a 409 (sent announcements are immutable)."""
        with transaction.atomic():
            instance = get_object_or_404(self.get_queryset().select_for_update(), pk=kwargs["pk"])
            if instance.status == Announcement.Status.SENT:
                raise Conflict("A sent announcement cannot be edited.")
            serializer = self.get_serializer(instance, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save(status=Announcement.Status.DRAFT, approved_at=None)
        return Response(serializer.data)


class AnnouncementDraftView(APIView):
    """POST /api/announcements/draft/ -- ask the AI provider for a draft.

    Success: 201 and a stored announcement with status=draft (never approved, never sent).
    Provider down, slow or returning unusable text (after one retry): 503 with
    fallback=manual and nothing stored; the leader writes it with POST /api/announcements/.
    """

    permission_classes = [IsLeader]

    def post(self, request):
        data = DraftRequestSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            provider_name, draft = ai.generate_draft(data.validated_data["prompt"])
        except ai.DraftUnavailable:  # reason is logged server-side, not returned
            return Response(
                {
                    "detail": "AI drafting is unavailable right now. Please write the announcement manually.",
                    "fallback": "manual",
                    "manual_endpoint": "/api/announcements/",
                },
                status=503,
            )
        user = request.user
        announcement = Announcement.objects.create(
            local_id=user.local_id,
            created_by=user,
            status=Announcement.Status.DRAFT,
            title=draft.title,
            body=draft.body,
            push_preview=draft.push_preview,
            needs_ack=data.validated_data["needs_ack"],
            audience_classifications=data.validated_data["audience_classifications"],
        )
        return Response(
            {"source": "ai", "provider": provider_name, "announcement": AnnouncementSerializer(announcement).data},
            status=201,
        )


class AnnouncementApproveView(APIView):
    """POST /api/announcements/<id>/approve/ -- the explicit human approval step.

    draft -> approved with approved_at, in one conditional UPDATE. Repeating it is a
    no-op that returns the existing approval. A sent announcement returns 409. The
    notification text (push_preview) must exist, because it is what members are pushed.
    """

    permission_classes = [IsLeader]

    def post(self, request, pk):
        announcement = get_object_or_404(Announcement.objects.for_account(request.user), pk=pk)
        won = (
            Announcement.objects.filter(
                pk=announcement.pk, local_id=announcement.local_id, status=Announcement.Status.DRAFT
            )
            .exclude(push_preview="")
            .update(status=Announcement.Status.APPROVED, approved_at=timezone.now())
        )
        announcement.refresh_from_db()
        if not won:
            if announcement.status == Announcement.Status.SENT:
                raise Conflict("This announcement has already been sent.")
            if announcement.status != Announcement.Status.APPROVED:
                raise ValidationError({"push_preview": "A notification text is required before approval."})
        return Response(dict(AnnouncementSerializer(announcement).data, already_approved=not won))


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
        if not won and announcement.status != Announcement.Status.SENT:
            # approval was withdrawn (the announcement was edited) between the check and the send
            raise Conflict("Announcement is not approved. Approve it before sending.")

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
