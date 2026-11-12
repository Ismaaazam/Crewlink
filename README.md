# CrewLink
Union leaders draft (AI or manual), approve and send announcements; members get a durable inbox and read/ack. Django + DRF + Postgres, Next.js leader UI. Design, limits and cuts: [DESIGN.md](DESIGN.md).

## Run (needs Docker; from the repo root)
```
cd crewlink && cp .env.example .env      # set POSTGRES_PASSWORD
docker compose up -d --build && docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py seed        # idempotent
docker compose exec backend python manage.py run_worker  # push worker (--once to drain and exit)
```
Leader UI: http://localhost:3000 · API: http://localhost:8000/api · AI defaults to a fake provider (no key needed).

## Test Accounts
```json
[
  {"username": "leader.local27", "role": "leader", "local": "Local 27", "password": "crewlink-demo-pass"},
  {"username": "member.local27", "role": "member", "local": "Local 27", "password": "crewlink-demo-pass"},
  {"username": "leader.local8",  "role": "leader", "local": "Local 8",  "password": "crewlink-demo-pass"},
  {"username": "member.local8",  "role": "member", "local": "Local 8",  "password": "crewlink-demo-pass"}
]
```

## Member read/ack, then cross-local check (bash / Git Bash)
```
API=http://localhost:8000/api; H='Content-Type: application/json'; A=1a6c4b65-76d8-51df-9ad4-59577c77afea   # seeded Local 27 announcement
tok(){ curl -s $API/auth/login/ -H "$H" -d "{\"username\":\"$1\",\"password\":\"crewlink-demo-pass\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])'; }
M=$(tok member.local27); curl -s $API/me/inbox/ -H "Authorization: Token $M"              # own inbox only
curl -s -X POST $API/me/inbox/$A/read/ -H "Authorization: Token $M"                        # read_at set once; repeats unchanged
curl -s -X POST $API/me/inbox/$A/ack/  -H "Authorization: Token $M"                        # acked_at set once
L8=$(tok leader.local8); L27=$(tok leader.local27)
ID8=$(curl -s -X POST $API/announcements/ -H "$H" -H "Authorization: Token $L8" -d '{"title":"t","body":"b","push_preview":"p"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -o /dev/null -w '%{http_code}\n' $API/announcements/$ID8/ -H "Authorization: Token $L27"   # 404: Local 27 cannot see Local 8
curl -s -o /dev/null -w '%{http_code}\n' $API/announcements/ -H "Authorization: Token $M"          # 403: member cannot use leader endpoints
```
Leader flow: `POST /announcements/` (or `/announcements/draft/`) → `/<id>/approve/` → `/<id>/send/` (202 first time, 200 `already_sent` on retry) → `GET /<id>/progress/`.

## Rule 2 (no double delivery) verification
`docker compose exec backend python manage.py test core.test_send_worker` runs on Postgres with real threads: double send, 8 concurrent sends, 4 concurrent workers, worker run twice, expired-lease recovery. Whole suite: `docker compose exec backend python manage.py test` (116 tests).
