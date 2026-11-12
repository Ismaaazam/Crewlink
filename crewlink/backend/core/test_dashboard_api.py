from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Account, Announcement, AnnouncementRecipient, Local, Member

PW = "pw-12345-test"


def make_account(local, username, role, classification="Electrician", status="active"):
    member = Member.objects.create(
        local=local, full_name=username, email=f"{username}@example.test",
        classification=classification, status=status,
    )
    return Account.objects.create_user(username=username, password=PW, member=member, role=role)


class DashboardApiTests(TestCase):
    def setUp(self):
        self.l1 = Local.objects.create(name="Local 1")
        self.l2 = Local.objects.create(name="Local 2")
        self.leader = make_account(self.l1, "lead1", "leader", "Electrician")
        self.member = make_account(self.l1, "mem1", "member", "Welder")
        make_account(self.l1, "retired1", "member", "Retired-Class", status="retired")
        self.leader2 = make_account(self.l2, "lead2", "leader", "OtherLocalClass")
        self.ann = Announcement.objects.create(
            local=self.l1, created_by=self.leader, title="t", body="b", push_preview="p",
            status="sent", sent_at=timezone.now(), approved_at=timezone.now(),
        )
        now = timezone.now()
        AnnouncementRecipient.objects.create(announcement=self.ann, member=self.leader.member, local=self.l1, sent_at=now, read_at=now, acked_at=now)
        AnnouncementRecipient.objects.create(announcement=self.ann, member=self.member.member, local=self.l1, sent_at=now, read_at=now)

    def client_for(self, account=None):
        c = APIClient()
        if account:
            c.force_authenticate(account)
        return c

    def test_audiences_only_own_local_and_active(self):
        r = self.client_for(self.leader).get("/api/announcements/audiences/")
        self.assertEqual(r.status_code, 200)
        names = [c["classification"] for c in r.json()["classifications"]]
        self.assertEqual(names, ["Electrician", "Welder"])
        self.assertEqual(r.json()["local"]["name"], "Local 1")

    def test_audiences_denied_to_members_and_anonymous(self):
        self.assertEqual(self.client_for(self.member).get("/api/announcements/audiences/").status_code, 403)
        self.assertEqual(self.client_for().get("/api/announcements/audiences/").status_code, 401)

    def test_progress_counts(self):
        r = self.client_for(self.leader).get(f"/api/announcements/{self.ann.id}/progress/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["counts"], {"recipients": 2, "pushed": 2, "read": 2, "acknowledged": 1})

    def test_progress_other_local_is_404(self):
        r = self.client_for(self.leader2).get(f"/api/announcements/{self.ann.id}/progress/")
        self.assertEqual(r.status_code, 404)

    def test_progress_denied_to_members_and_anonymous(self):
        url = f"/api/announcements/{self.ann.id}/progress/"
        self.assertEqual(self.client_for(self.member).get(url).status_code, 403)
        self.assertEqual(self.client_for().get(url).status_code, 401)
