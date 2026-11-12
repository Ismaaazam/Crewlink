from rest_framework import serializers

from .models import Announcement


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
