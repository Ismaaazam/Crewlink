from django.contrib.auth import authenticate
from django.db import connection
from django.db.models import Count, Q
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
from .serializers import AnnouncementSerializer, LoginSerializer


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
