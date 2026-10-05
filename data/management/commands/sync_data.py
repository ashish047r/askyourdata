import datetime as dt

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from data.google_api import fetch_ads_campaign_daily, fetch_ga4_daily, get_credentials
from data.models import Client
from data.sync import upsert_ads, upsert_ga4


class Command(BaseCommand):
    help = "Pull GA4 + Google Ads daily data into Postgres (idempotent upsert). Ends yesterday."

    def add_arguments(self, parser):
        parser.add_argument("--client", help="client slug (default: all active clients with API ids)")
        parser.add_argument("--days", type=int, default=90)

    def handle(self, *args, client=None, days=90, **opts):
        clients = Client.objects.filter(is_active=True)
        if client:
            clients = clients.filter(slug=client)
        clients = clients.exclude(ga4_property_id="", ads_customer_id="")
        if not clients:
            raise CommandError("No matching clients with a GA4 property or Ads customer id.")

        end = dt.date.today() - dt.timedelta(days=1)
        start = end - dt.timedelta(days=days - 1)
        creds = get_credentials()
        failed = []
        for c in clients:
            try:
                ga4 = upsert_ga4(c, fetch_ga4_daily(c.ga4_property_id, start, end, creds)) if c.ga4_property_id else 0
                ads = 0
                if c.ads_customer_id:
                    rows, currency = fetch_ads_campaign_daily(c.ads_customer_id, c.ads_manager_id, start, end, creds)
                    ads = upsert_ads(c, rows)
                    c.currency_code = currency or c.currency_code
                c.last_synced_at = timezone.now()  # changes data_version -> old cache entries stop matching
                c.save(update_fields=["last_synced_at", "currency_code"])
                self.stdout.write(f"{c.slug}: ga4={ga4} ads={ads} rows ({start}..{end})")
            except Exception as e:  # one broken client must not stop the others
                failed.append(c.slug)
                self.stderr.write(f"{c.slug}: FAILED {e}")
        if failed:
            raise CommandError(f"sync failed for: {', '.join(failed)}")
