from django.db import migrations

# Django cannot express composite foreign keys, so they are added in SQL.
# They make cross-local links impossible at the database level: a row's local_id must
# equal the local_id of the member / announcement it references.

FORWARD = [
    """ALTER TABLE accounts ADD CONSTRAINT accounts_member_local_fk
       FOREIGN KEY (member_id, local_id) REFERENCES members (id, local_id);""",
    """ALTER TABLE announcement_recipients ADD CONSTRAINT recipients_member_local_fk
       FOREIGN KEY (member_id, local_id) REFERENCES members (id, local_id);""",
    """ALTER TABLE announcement_recipients ADD CONSTRAINT recipients_announcement_local_fk
       FOREIGN KEY (announcement_id, local_id) REFERENCES announcements (id, local_id);""",
]
REVERSE = [
    "ALTER TABLE announcement_recipients DROP CONSTRAINT recipients_announcement_local_fk;",
    "ALTER TABLE announcement_recipients DROP CONSTRAINT recipients_member_local_fk;",
    "ALTER TABLE accounts DROP CONSTRAINT accounts_member_local_fk;",
]


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE)]
