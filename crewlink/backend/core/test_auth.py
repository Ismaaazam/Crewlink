import uuid
from types import SimpleNamespace

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .checks import check_endpoint_permissions
from .models import Account, Announcement, Local, Member

PASSWORD = "pw-test"
FAST_HASH = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # speed only, tests only


def make_local(number):
    local = Local.objects.create(name=f"Local {number}")

    def person(kind):
        member = Member.objects.create(
            local=local, full_name=f"{kind} {number}", email=f"{kind}@local{number}.example",
            classification="Journeyman Wireman")
        return Account.objects.create_user(f"{kind}.local{number}", PASSWORD, member, kind)

    leader, member = person("leader"), person("member")
    now = timezone.now()
    draft = Announcement.objects.create(local=local, title=f"Draft {number}", body="b", created_by=leader)
    sent = Announcement.objects.create(
        local=local, title=f"Sent {number}", body="b", status="sent",
        approved_at=now, sent_at=now, created_by=leader)
    return SimpleNamespace(local=local, leader=leader, member=member, draft=draft, sent=sent)


@override_settings(PASSWORD_HASHERS=FAST_HASH)
class ApiTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.l27 = make_local(27)
        cls.l28 = make_local(28)

    def as_user(self, account, **extra):
        client = APIClient()
        resp = client.post("/api/auth/login/", {"username": account.username, "password": PASSWORD}, format="json")
        self.assertEqual(resp.status_code, 200)
        client.credentials(HTTP_AUTHORIZATION=f"Token {resp.json()['token']}", **extra)
        return client

    def url(self, announcement):
        return f"/api/announcements/{announcement.id}/"


class LoginTests(ApiTestCase):
    def test_login_returns_token_role_and_local(self):
        resp = APIClient().post("/api/auth/login/", {"username": "leader.local27", "password": PASSWORD}, format="json")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["token"])
        self.assertEqual(body["role"], "leader")
        self.assertEqual(body["local"]["name"], "Local 27")

    def test_bad_credentials_look_identical(self):
        c = APIClient()
        wrong = c.post("/api/auth/login/", {"username": "leader.local27", "password": "nope"}, format="json")
        unknown = c.post("/api/auth/login/", {"username": "ghost", "password": "nope"}, format="json")
        self.assertEqual((wrong.status_code, wrong.json()), (401, {"detail": "Invalid credentials."}))
        self.assertEqual((unknown.status_code, unknown.json()), (wrong.status_code, wrong.json()))

    def test_inactive_account_cannot_log_in(self):
        Account.objects.filter(pk=self.l27.member.pk).update(is_active=False)
        resp = APIClient().post("/api/auth/login/", {"username": "member.local27", "password": PASSWORD}, format="json")
        self.assertEqual(resp.status_code, 401)

    def test_anonymous_and_garbage_token_are_rejected(self):
        c = APIClient()
        for path in ("/api/me/", "/api/announcements/", self.url(self.l27.draft)):
            self.assertEqual(c.get(path).status_code, 401, path)
        c.credentials(HTTP_AUTHORIZATION="Token not-a-real-token")
        self.assertEqual(c.get("/api/me/").status_code, 401)


class CrossLocalIsolationTests(ApiTestCase):
    def test_1_cross_local_read_fails(self):
        c = self.as_user(self.l27.leader)
        titles = {a["title"] for a in c.get("/api/announcements/").json()["results"]}
        self.assertEqual(titles, {"Draft 27", "Sent 27"})  # nothing from Local 28
        self.assertEqual(c.get(self.url(self.l28.draft)).status_code, 404)
        self.assertEqual(c.get(self.url(self.l28.sent)).status_code, 404)

    def test_3_outside_scope_is_404_and_indistinguishable_from_nonexistent(self):
        c = self.as_user(self.l27.leader)
        other = c.get(self.url(self.l28.draft))
        missing = c.get(f"/api/announcements/{uuid.uuid4()}/")
        self.assertEqual(other.status_code, 404)
        self.assertEqual((other.status_code, other.json()), (missing.status_code, missing.json()))
        # same for modification attempts
        self.assertEqual(c.patch(self.url(self.l28.draft), {"title": "x"}, format="json").status_code, 404)
        self.assertEqual(c.patch(f"/api/announcements/{uuid.uuid4()}/", {"title": "x"}, format="json").status_code, 404)

    def test_cross_local_modify_fails_and_changes_nothing(self):
        c = self.as_user(self.l27.leader)
        c.patch(self.url(self.l28.draft), {"title": "hijacked"}, format="json")
        self.l28.draft.refresh_from_db()
        self.assertEqual(self.l28.draft.title, "Draft 28")

    def test_isolation_is_symmetric(self):
        c = self.as_user(self.l28.leader)
        self.assertEqual(c.get(self.url(self.l27.draft)).status_code, 404)
        titles = {a["title"] for a in c.get("/api/announcements/").json()["results"]}
        self.assertEqual(titles, {"Draft 28", "Sent 28"})

    def test_create_cannot_target_another_local_or_set_lifecycle_fields(self):
        c = self.as_user(self.l27.leader)
        resp = c.post("/api/announcements/", {
            "title": "t", "body": "b", "local_id": str(self.l28.local.id),
            "status": "sent", "sent_at": "2026-01-01T00:00:00Z", "approved_at": "2026-01-01T00:00:00Z",
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        created = Announcement.objects.get(pk=resp.json()["id"])
        self.assertEqual(created.local_id, self.l27.local.id)
        self.assertEqual((created.status, created.sent_at, created.approved_at), ("draft", None, None))
        self.assertEqual(Announcement.objects.filter(local=self.l28.local).count(), 2)  # unchanged


class RoleSeparationTests(ApiTestCase):
    def test_2_member_cannot_call_leadership_endpoints(self):
        c = self.as_user(self.l27.member)
        own, other = self.url(self.l27.draft), self.url(self.l28.draft)
        self.assertEqual(c.get("/api/announcements/").status_code, 403)
        self.assertEqual(c.post("/api/announcements/", {"title": "t", "body": "b"}, format="json").status_code, 403)
        for path in (own, other):
            self.assertEqual(c.get(path).status_code, 403)
            self.assertEqual(c.patch(path, {"title": "x"}, format="json").status_code, 403)
        self.assertEqual(Announcement.objects.filter(title="t").count(), 0)
        self.l27.draft.refresh_from_db()
        self.assertEqual(self.l27.draft.title, "Draft 27")

    def test_role_comes_from_the_database_not_the_request(self):
        c = self.as_user(self.l27.member, HTTP_X_ROLE="leader")
        self.assertEqual(c.get("/api/announcements/").status_code, 403)

    def test_demotion_takes_effect_on_the_next_request(self):
        c = self.as_user(self.l27.leader)
        self.assertEqual(c.get("/api/announcements/").status_code, 200)
        Account.objects.filter(pk=self.l27.leader.pk).update(role="member")
        self.assertEqual(c.get("/api/announcements/").status_code, 403)

    def test_member_can_use_member_functionality(self):
        body = self.as_user(self.l27.member).get("/api/me/").json()
        self.assertEqual((body["username"], body["role"], body["local"]["name"]),
                         ("member.local27", "member", "Local 27"))
        self.assertEqual(body["member"]["email"], "member@local27.example")  # own row only


class AuthorizedAccessTests(ApiTestCase):
    def test_4_leader_can_use_own_local(self):
        c = self.as_user(self.l27.leader)
        self.assertEqual(c.get("/api/me/").json()["role"], "leader")
        self.assertEqual(c.get("/api/announcements/").json()["count"], 2)
        self.assertEqual(c.get(self.url(self.l27.draft)).status_code, 200)

        created = c.post("/api/announcements/", {"title": "New", "body": "Body", "needs_ack": True}, format="json")
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["status"], "draft")
        self.assertEqual(Announcement.objects.get(pk=created.json()["id"]).created_by_id, self.l27.leader.id)

        patched = c.patch(self.url(self.l27.draft), {"title": "Renamed"}, format="json")
        self.assertEqual((patched.status_code, patched.json()["title"]), (200, "Renamed"))

    def test_sent_announcements_cannot_be_edited(self):
        c = self.as_user(self.l27.leader)
        self.assertEqual(c.patch(self.url(self.l27.sent), {"title": "x"}, format="json").status_code, 409)

    def test_push_preview_over_120_is_rejected_by_the_api(self):
        c = self.as_user(self.l27.leader)
        resp = c.post("/api/announcements/", {"title": "t", "body": "b", "push_preview": "x" * 121}, format="json")
        self.assertEqual(resp.status_code, 400)


class DefaultDenyTests(ApiTestCase):
    def test_real_urlconf_passes_the_startup_check(self):
        self.assertEqual(check_endpoint_permissions(), [])

    def test_check_flags_undeclared_leaky_and_non_drf_routes(self):
        with override_settings(ROOT_URLCONF="core.urls_undeclared_for_tests"):
            ids = sorted(e.id for e in check_endpoint_permissions())
        self.assertEqual(ids, ["core.E001", "core.E002", "core.E004"])

    def test_check_flags_a_weakened_default(self):
        weak = {"DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"]}
        with override_settings(REST_FRAMEWORK=weak):
            self.assertIn("core.E003", [e.id for e in check_endpoint_permissions()])

    def test_endpoint_without_declared_policy_is_denied_at_runtime(self):
        c = self.as_user(self.l27.leader)  # log in under the real urlconf first
        with override_settings(ROOT_URLCONF="core.urls_undeclared_for_tests"):
            self.assertEqual(c.get("/api/forgot/").status_code, 403)       # even for a leader
            self.assertEqual(APIClient().get("/api/forgot/").status_code, 401)
