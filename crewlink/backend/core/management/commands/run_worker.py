import logging
import os
import socket
import time
import uuid

from django.core.management.base import BaseCommand

from core.worker import process_available


class Command(BaseCommand):
    help = "Claim announcement recipients in batches and simulate push delivery."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="drain the queue once, then exit")
        parser.add_argument("--batch-size", type=int, default=200)
        parser.add_argument("--lease-seconds", type=int, default=60)
        parser.add_argument("--poll-seconds", type=float, default=2.0)
        parser.add_argument("--worker-id", default=None)

    def handle(self, *args, **opts):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
        worker_id = opts["worker_id"] or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.stdout.write(f"worker {worker_id} started")
        while True:
            totals = process_available(worker_id, opts["batch_size"], opts["lease_seconds"])
            if totals["claimed"]:
                self.stdout.write(f"worker {worker_id}: {totals}")
            if opts["once"]:
                return
            try:
                time.sleep(opts["poll_seconds"])
            except KeyboardInterrupt:
                self.stdout.write("stopping")
                return
