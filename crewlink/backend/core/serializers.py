from rest_framework import serializers

from .models import Announcement, AnnouncementRecipient


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)


class AnnouncementSerializer(serializers.ModelSerializer):
    # Everything that decides scope or lifecycle is read-only: the client cannot choose
    # the local, the status, or the timestamps. push_preview max_length=120 comes from the model.
    local_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Announcement
        fields = [
            "id", "local_id", "title", "body", "needs_ack", "status", "push_preview",
            "audience_classifications", "approved_at", "sent_at", "created_at",
        ]
        read_only_fields = ["id", "status", "approved_at", "sent_at", "created_at"]


class InboxItemSerializer(serializers.ModelSerializer):
    """What a member sees: the announcement and their OWN state. Deliberately no member
    ids, emails or other recipients, so no endpoint exposes anyone else's details."""

    announcement_id = serializers.UUIDField(read_only=True)
    title = serializers.CharField(source="announcement.title", read_only=True)
    body = serializers.CharField(source="announcement.body", read_only=True)
    push_preview = serializers.CharField(source="announcement.push_preview", read_only=True)
    needs_ack = serializers.BooleanField(source="announcement.needs_ack", read_only=True)
    sent_at = serializers.DateTimeField(source="announcement.sent_at", read_only=True)

    class Meta:
        model = AnnouncementRecipient
        fields = [
            "announcement_id", "title", "body", "push_preview", "needs_ack",
            "sent_at", "read_at", "acked_at",
        ]
        read_only_fields = fields
