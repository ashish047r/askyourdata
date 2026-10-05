import datetime as dt
import random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from data.models import AdsCampaignDaily, Client, Ga4Daily

DAYS = 120
SOURCES = [  # source, medium, base sessions/day, key-event rate
    ("google", "organic", 400, 0.015), ("google", "cpc", 250, 0.03), ("(direct)", "(none)", 150, 0.02),
    ("linkedin.com", "referral", 40, 0.01), ("newsletter", "email", 60, 0.04), ("chatgpt.com", "referral", 15, 0.05),
]
DEVICES = [("desktop", 0.55), ("mobile", 0.40), ("tablet", 0.05)]
CAMPAIGNS = [  # id, name, status, channel, clicks/day, cpc, conv rate, ctr
    (1001, "Brand - Search", "ENABLED", "SEARCH", 120, 0.6, 0.12, 0.18),
    (1002, "Generic - Search", "ENABLED", "SEARCH", 200, 2.1, 0.04, 0.05),
    (1003, "PMax - Leads", "ENABLED", "PERFORMANCE_MAX", 150, 1.2, 0.05, 0.02),
    (1004, "Display - Remarketing", "ENABLED", "DISPLAY", 80, 0.4, 0.02, 0.006),
    (1005, "YouTube - Awareness", "PAUSED", "VIDEO", 60, 0.15, 0.005, 0.01),
]


def seed_client(name, slug, rng_seed, scale):
    """Deterministic synthetic data (relative to today) so evals and demos always have recent rows."""
    rng = random.Random(rng_seed)
    client, _ = Client.objects.update_or_create(slug=slug, defaults={"name": name, "currency_code": "INR"})
    Ga4Daily.objects.filter(client=client).delete()
    AdsCampaignDaily.objects.filter(client=client).delete()
    today = dt.date.today()
    ga4, ads = [], []
    for i in range(DAYS, 0, -1):
        day = today - dt.timedelta(days=i)
        factor = scale * (0.7 if day.weekday() >= 5 else 1.0) * (1 + 0.002 * (DAYS - i))
        for source, medium, base, rate in SOURCES:
            if source == "linkedin.com" and i <= 7:
                factor_s = factor * 0.4  # a visible recent drop for week-over-week questions
            else:
                factor_s = factor
            for device, share in DEVICES:
                sessions = max(1, int(base * factor_s * share * rng.uniform(0.8, 1.2)))
                users = int(sessions * 0.8)
                key_events = float(round(sessions * rate * rng.uniform(0.7, 1.3)))
                ga4.append(Ga4Daily(
                    client=client, date=day, session_source=source, session_medium=medium, device_category=device,
                    sessions=sessions, total_users=users, new_users=int(users * 0.6),
                    engaged_sessions=int(sessions * rng.uniform(0.45, 0.7)), key_events=key_events,
                    total_revenue=round(key_events * rng.uniform(800, 1500), 2)))
        for cid, cname, status, channel, clicks_base, cpc, cvr, ctr in CAMPAIGNS:
            if status == "PAUSED" and i < 60:
                continue  # paused 60 days ago
            clicks = int(clicks_base * factor * rng.uniform(0.8, 1.2))
            spike = 1.6 if cname == "Generic - Search" and i <= 7 else 1.0  # recent CPC spike
            cost = round(clicks * cpc * spike * rng.uniform(0.9, 1.1) * 85, 2)  # INR
            conversions = round(clicks * cvr * rng.uniform(0.6, 1.4), 1)
            ads.append(AdsCampaignDaily(
                client=client, date=day, campaign_id=cid, campaign_name=cname, campaign_status=status,
                channel_type=channel, impressions=int(clicks / ctr), clicks=clicks, cost=cost,
                conversions=conversions, conversions_value=round(conversions * rng.uniform(3000, 6000), 2)))
    Ga4Daily.objects.bulk_create(ga4, batch_size=2000)
    AdsCampaignDaily.objects.bulk_create(ads, batch_size=2000)
    client.last_synced_at = timezone.now()
    client.save()
    return client, len(ga4), len(ads)


class Command(BaseCommand):
    help = "Create two synthetic clients (demo, acme) for demos, tests and CI evals. Safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="username to give access to both demo clients")

    def handle(self, *args, user=None, **opts):
        for name, slug, seed, scale in (("Demo Co", "demo", 42, 1.0), ("Acme Labs", "acme", 7, 0.5)):
            client, g, a = seed_client(name, slug, seed, scale)
            if user:
                client.users.add(get_user_model().objects.get(username=user))
            self.stdout.write(f"{slug}: {g} ga4 rows, {a} ads rows")
