"""
Data model.

The brief supplied `locals`, `members` and `announcements`; their names and columns
are kept as-is and only extended. New tables: `accounts`, `announcement_recipients`.

Tenant integrity is enforced in the database, not only in code: migration 0002 adds
composite foreign keys so a row's local_id must match the local_id of the member /
announcement it points at.
"""
import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.utils import timezone


class LocalScopedQuerySet(models.QuerySet):
    """Every view reads tenant data through for_account(); the local comes from the
    authenticated account, never from the request."""

    def for_account(self, account):
        return self.filter(local_id=account.local_id)


class Local(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.TextField(unique=True)

    class Meta:
        db_table = "locals"

    def __str__(self):
        return self.name


class Member(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active"
        RETIRED = "retired"
        SUSPENDED = "suspended"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    local = models.ForeignKey(Local, on_delete=models.PROTECT, related_name="members")
    full_name = models.TextField()
    email = models.TextField()
    classification = models.TextField()
    status = models.TextField(choices=Status.choices, default=Status.ACTIVE)

    objects = LocalScopedQuerySet.as_manager()

    class Meta:
        db_table = "members"
        constraints = [
            models.UniqueConstraint(fields=["local", "email"], name="members_local_email_uniq"),
            # target of the composite foreign keys added in migration 0002
            models.UniqueConstraint(fields=["id", "local"], name="members_id_local_uniq"),
            models.CheckConstraint(
                condition=Q(status__in=["active", "retired", "suspended"]),
                name="members_status_valid",
            ),
        ]
        indexes = [models.Index(fields=["local", "status"], name="members_local_status_idx")]

    def __str__(self):
        return f"{self.full_name} ({self.local_id})"


class AccountManager(BaseUserManager):
    def create_user(self, username, password, member, role, **extra):
        account = self.model(
            username=username, member=member, local_id=member.local_id, role=role, **extra
        )
        account.set_password(password)
        account.save(using=self._db)
        return account


class Account(AbstractBaseUser):
    """A login. Every account belongs to exactly one member; local_id is copied from
    that member and the database refuses any mismatch (composite FK in 0002)."""

    class Role(models.TextChoices):
        MEMBER = "member"
        LEADER = "leader"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = models.CharField(max_length=150, unique=True)
    member = models.OneToOneField(Member, on_delete=models.PROTECT, related_name="account")
    local = models.ForeignKey(Local, on_delete=models.PROTECT, related_name="accounts")
    role = models.TextField(choices=Role.choices, default=Role.MEMBER)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    USERNAME_FIELD = "username"
    objects = AccountManager()

    class Meta:
        db_table = "accounts"
        constraints = [
            models.CheckConstraint(
                condition=Q(role__in=["member", "leader"]), name="accounts_role_valid"
            ),
        ]

    @property
    def is_leader(self):
        return self.role == self.Role.LEADER

    def __str__(self):
        return self.username


class Announcement(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft"
        APPROVED = "approved"
        SENT = "sent"

    # --- columns supplied by the brief ---
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    local = models.ForeignKey(Local, on_delete=models.PROTECT, related_name="announcements")
    title = models.TextField()
    body = models.TextField()
    needs_ack = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True, blank=True)  # null until sent
    created_at = models.DateTimeField(default=timezone.now)
    # --- additions ---
    status = models.TextField(choices=Status.choices, default=Status.DRAFT)
    push_preview = models.CharField(max_length=120, blank=True, default="")  # DB-level 120 cap
    approved_at = models.DateTimeField(null=True, blank=True)
    # empty list = whole local
    audience_classifications = ArrayField(models.TextField(), default=list, blank=True)
    created_by = models.ForeignKey(
        Account, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    objects = LocalScopedQuerySet.as_manager()

    class Meta:
        db_table = "announcements"
        constraints = [
            models.UniqueConstraint(fields=["id", "local"], name="announcements_id_local_uniq"),
            models.CheckConstraint(
                condition=Q(status__in=["draft", "approved", "sent"]),
                name="announcements_status_valid",
            ),
            # nothing leaves draft without a recorded human approval
            models.CheckConstraint(
                condition=Q(status="draft") | Q(approved_at__isnull=False),
                name="announcements_approved_has_timestamp",
            ),
            # sent_at is set exactly when status is 'sent'
            models.CheckConstraint(
                condition=Q(status="sent", sent_at__isnull=False)
                | (~Q(status="sent") & Q(sent_at__isnull=True)),
                name="announcements_sent_at_matches_status",
            ),
        ]
        indexes = [models.Index(fields=["local", "-created_at"], name="announcements_local_recent_idx")]

    def __str__(self):
        return self.title


class AnnouncementRecipient(models.Model):
    """One row per (announcement, member). Doubles as the work queue for the worker:
    a row is claimable while sent_at IS NULL and its lease is empty or expired."""

    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="recipients")
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="inbox_items")
    local = models.ForeignKey(Local, on_delete=models.PROTECT, related_name="+")
    claimed_by = models.CharField(max_length=100, null=True, blank=True)  # worker id
    lease_until = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)  # push accepted by provider
    read_at = models.DateTimeField(null=True, blank=True)
    acked_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)

    objects = LocalScopedQuerySet.as_manager()

    class Meta:
        db_table = "announcement_recipients"
        constraints = [
            # Rule 2 at the schema level: one recipient row per member per announcement
            models.UniqueConstraint(
                fields=["announcement", "member"], name="recipients_announcement_member_uniq"
            ),
            # a claim is a worker id plus a lease, set and cleared together
            models.CheckConstraint(
                condition=Q(claimed_by__isnull=True, lease_until__isnull=True)
                | Q(claimed_by__isnull=False, lease_until__isnull=False),
                name="recipients_claim_fields_together",
            ),
        ]
        indexes = [
            models.Index(
                fields=["announcement"],
                condition=Q(sent_at__isnull=True),
                name="recipients_unsent_idx",
            ),
            models.Index(fields=["member"], name="recipients_member_idx"),
        ]
