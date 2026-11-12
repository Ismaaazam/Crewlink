"""
Idempotent demo seed:  python manage.py seed

- 43 locals ("Local 1" .. "Local 43")
- Local 27: 2,000 members.  Local 8: 200 members.  (The brief seeds two locals.)
- 4 classifications, a mix of active / retired / suspended members
- per seeded local: one leader login and one member login
- one already-sent announcement in Local 27, with recipient rows

Every id is derived from a fixed namespace (uuid5), and inserts use ON CONFLICT DO
NOTHING, so running it again creates nothing new.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Account, Announcement, AnnouncementRecipient, Local, Member

NAMESPACE = uuid.UUID("6f1d0a52-6a52-4c0e-9a0e-3f6c2f0c7a11")
DEMO_PASSWORD = os.environ.get("SEED_PASSWORD", "crewlink-demo-pass")

FIRST = ["James", "Maria", "Luis", "Aisha", "Tom", "Grace", "Omar", "Elena", "Dev", "Nina",
         "Carl", "Rosa", "Ken", "Ivy", "Hugo", "Lena", "Raj", "Tess", "Abel", "June"]
LAST = ["Walker", "Santos", "Kim", "Nguyen", "Brown", "Patel", "Rossi", "Haddad", "Cole",
        "Ivanov", "Murphy", "Diaz", "Lee", "Foster", "Khan", "Bauer", "Okoye", "Silva",
        "Moore", "Chen", "Reid", "Vega", "Hall", "Boyd", "Young"]

# 4 classifications; weights add up to 20 buckets (55 / 20 / 15 / 10 %)
CLASSIFICATIONS = (
    ["Journeyman Wireman"] * 11 + ["Apprentice 3rd Year"] * 4
    + ["Apprentice 1st Year"] * 3 + ["Foreman"] * 2
)

SEEDED_LOCALS = [
    {"number": 27, "size": 2000,
     "leader": ("leader.local27", "Denise Okafor", "denise.okafor"),
     "member": ("member.local27", "Jordan Ellis", "jordan.ellis")},
    {"number": 8, "size": 200,
     "leader": ("leader.local8", "Marcus Reyes", "marcus.reyes"),
     "member": ("member.local8", "Priya Nair", "priya.nair")},
]

SENT_AT = datetime(2026, 11, 9, 14, 2, tzinfo=timezone.utc)


def uid(*parts):
    return uuid.uuid5(NAMESPACE, "/".join(parts))


def member_status(i):
    # i = 0 and 1 are the two logins and always stay active
    if i >= 2 and i % 20 == 0:
        return Member.Status.RETIRED
    if i >= 2 and i % 25 == 13:
        return Member.Status.SUSPENDED
    return Member.Status.ACTIVE


class Command(BaseCommand):
    help = "Seed demo data (safe to run repeatedly)."

    @transaction.atomic
    def handle(self, *args, **opts):
        before = self.counts()

        Local.objects.bulk_create(
            [Local(id=uid("local", str(n)), name=f"Local {n}") for n in range(1, 44)],
            ignore_conflicts=True,
        )

        for spec in SEEDED_LOCALS:
            self.seed_local(spec)
        self.seed_announcement()

        after = self.counts()
        self.stdout.write("table                    before    after")
        for key in after:
            self.stdout.write(f"{key:<22} {before[key]:>8} {after[key]:>8}")
        self.stdout.write(self.style.SUCCESS("Seed complete."))

    # ------------------------------------------------------------------
    def seed_local(self, spec):
        n = spec["number"]
        local_id = uid("local", str(n))
        members = []
        for i in range(spec["size"]):
            if i == 0:
                _, name, email_stub = spec["leader"]
            elif i == 1:
                _, name, email_stub = spec["member"]
            else:
                name = f"{FIRST[i % 20]} {LAST[(i // 20) % 25]}"
                email_stub = f"{name.lower().replace(' ', '.')}.{i:04d}"
            members.append(Member(
                id=uid("member", str(n), str(i)),
                local_id=local_id,
                full_name=name,
                email=f"{email_stub}@local{n}.example",
                classification=CLASSIFICATIONS[i % 20],
                status=member_status(i),
            ))
        Member.objects.bulk_create(members, ignore_conflicts=True, batch_size=1000)

        for index, role in ((0, Account.Role.LEADER), (1, Account.Role.MEMBER)):
            username = spec["leader" if index == 0 else "member"][0]
            if Account.objects.filter(username=username).exists():
                continue
            Account.objects.create_user(
                username=username,
                password=DEMO_PASSWORD,
                member=Member.objects.get(id=uid("member", str(n), str(index))),
                role=role,
                id=uid("account", username),
            )

    def seed_announcement(self):
        local_id = uid("local", "27")
        leader = Account.objects.get(username="leader.local27")
        announcement, _ = Announcement.objects.get_or_create(
            id=uid("announcement", "local27-emergency-meeting"),
            defaults=dict(
                local_id=local_id,
                title="Emergency meeting Thursday 6pm",
                body="Emergency meeting Thursday 6pm at the hall regarding the contractor "
                     "pulling crews off the westside job. Everyone needs to be there.",
                needs_ack=True,
                status=Announcement.Status.SENT,
                push_preview="Emergency meeting Thu 6pm at the hall - contractor pulling crews. Please confirm.",
                approved_at=SENT_AT - timedelta(minutes=2),
                sent_at=SENT_AT,
                created_at=SENT_AT - timedelta(minutes=5),
                audience_classifications=[],
                created_by=leader,
            ),
        )
        recipients = []
        for member in Member.objects.filter(local_id=local_id, status="active"):
            bucket = member.id.int % 10
            read_at = SENT_AT + timedelta(seconds=60 + member.id.int % 3000) if bucket < 6 else None
            acked_at = read_at + timedelta(seconds=30) if bucket < 3 else None
            recipients.append(AnnouncementRecipient(
                announcement=announcement, member=member, local_id=local_id,
                sent_at=SENT_AT + timedelta(seconds=3), read_at=read_at,
                acked_at=acked_at, attempts=1,
            ))
        AnnouncementRecipient.objects.bulk_create(recipients, ignore_conflicts=True, batch_size=1000)

    @staticmethod
    def counts():
        return {
            "locals": Local.objects.count(),
            "members": Member.objects.count(),
            "accounts": Account.objects.count(),
            "announcements": Announcement.objects.count(),
            "announcement_recipients": AnnouncementRecipient.objects.count(),
        }
