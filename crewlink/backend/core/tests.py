from django.core.management import call_command
from django.db import DataError, IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from .models import Account, Announcement, AnnouncementRecipient, Local, Member


class HealthTests(TestCase):
    def test_health_is_public_and_reports_database(self):
        resp = self.client.get("/api/health/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok", "database": "ok"})


class ConstraintTests(TestCase):
    def setUp(self):
        self.a = Local.objects.create(name="Local A")
        self.b = Local.objects.create(name="Local B")
        self.ma = Member.objects.create(local=self.a, full_name="A", email="a@x", classification="Foreman")
        self.mb = Member.objects.create(local=self.b, full_name="B", email="b@x", classification="Foreman")
        self.ann = Announcement.objects.create(local=self.a, title="t", body="b")

    def fails(self, exc, fn):
        with self.assertRaises(exc), transaction.atomic():
            fn()

    # --- Rule 2 at schema level ---
    def test_duplicate_recipient_rejected(self):
        AnnouncementRecipient.objects.create(announcement=self.ann, member=self.ma, local=self.a)
        self.fails(IntegrityError, lambda: AnnouncementRecipient.objects.create(
            announcement=self.ann, member=self.ma, local=self.a))

    # --- Rule 1 at schema level (composite foreign keys) ---
    def test_recipient_member_from_other_local_rejected(self):
        self.fails(IntegrityError, lambda: AnnouncementRecipient.objects.create(
            announcement=self.ann, member=self.mb, local=self.a))

    def test_recipient_local_must_match_announcement_local(self):
        self.fails(IntegrityError, lambda: AnnouncementRecipient.objects.create(
            announcement=self.ann, member=self.mb, local=self.b))

    def test_account_local_must_match_member_local(self):
        self.fails(IntegrityError, lambda: Account(
            username="y", member=self.ma, local=self.b, role="member").save())

    # --- other invariants ---
    def test_same_email_allowed_in_different_locals_only(self):
        Member.objects.create(local=self.b, full_name="A2", email="a@x", classification="Foreman")
        self.fails(IntegrityError, lambda: Member.objects.create(
            local=self.a, full_name="A3", email="a@x", classification="Foreman"))

    def test_invalid_member_status_rejected(self):
        self.fails(IntegrityError, lambda: Member.objects.create(
            local=self.a, full_name="Z", email="z@x", classification="c", status="banned"))

    def test_push_preview_capped_at_120_by_database(self):
        self.fails(DataError, lambda: Announcement.objects.create(
            local=self.a, title="t", body="b", push_preview="x" * 121))

    def test_sent_requires_sent_at_and_approval(self):
        now = timezone.now()
        self.fails(IntegrityError, lambda: Announcement.objects.create(
            local=self.a, title="t", body="b", status="sent", approved_at=now))  # no sent_at
        self.fails(IntegrityError, lambda: Announcement.objects.create(
            local=self.a, title="t", body="b", status="approved"))  # no approved_at
        self.fails(IntegrityError, lambda: Announcement.objects.create(
            local=self.a, title="t", body="b", status="draft", sent_at=now))  # sent_at on a draft

    def test_claim_fields_set_together(self):
        self.fails(IntegrityError, lambda: AnnouncementRecipient.objects.create(
            announcement=self.ann, member=self.ma, local=self.a, claimed_by="w1"))


class SeedTests(TestCase):
    def snapshot(self):
        return (Local.objects.count(), Member.objects.count(), Account.objects.count(),
                Announcement.objects.count(), AnnouncementRecipient.objects.count())

    def test_seed_is_idempotent_and_matches_brief(self):
        call_command("seed", verbosity=0)
        first = self.snapshot()
        call_command("seed", verbosity=0)
        self.assertEqual(first, self.snapshot())

        self.assertEqual(Local.objects.count(), 43)
        self.assertEqual(Member.objects.filter(local__name="Local 27").count(), 2000)
        self.assertEqual(Member.objects.filter(local__name="Local 8").count(), 200)
        self.assertEqual(Member.objects.values("classification").distinct().count(), 4)
        self.assertEqual(Account.objects.filter(role="leader").count(), 2)
        self.assertEqual(Account.objects.filter(role="member").count(), 2)
        for name in ("Local 27", "Local 8"):
            self.assertEqual(Account.objects.filter(local__name=name).count(), 2)

        ann = Announcement.objects.get()
        self.assertEqual(ann.status, "sent")
        self.assertEqual(ann.local.name, "Local 27")
        active = Member.objects.filter(local=ann.local, status="active").count()
        self.assertEqual(ann.recipients.count(), active)
        self.assertFalse(ann.recipients.exclude(local=ann.local).exists())
        self.assertFalse(ann.recipients.filter(acked_at__isnull=False, read_at__isnull=True).exists())
