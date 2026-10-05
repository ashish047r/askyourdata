from django.conf import settings
from django.db import models


class Client(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    ga4_property_id = models.CharField(max_length=32, blank=True, help_text="Digits only, e.g. 312345678")
    ads_customer_id = models.CharField(max_length=16, blank=True, help_text="Digits only, no dashes")
    ads_manager_id = models.CharField(max_length=16, blank=True, help_text="MCC id if accessed through a manager")
    currency_code = models.CharField(max_length=3, blank=True)
    is_active = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    users = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="clients")

    def __str__(self):
        return self.name


class Ga4Daily(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    date = models.DateField()
    session_source = models.CharField(max_length=255)
    session_medium = models.CharField(max_length=255)
    device_category = models.CharField(max_length=32)
    sessions = models.IntegerField()
    total_users = models.IntegerField()
    new_users = models.IntegerField()
    engaged_sessions = models.IntegerField()
    key_events = models.DecimalField(max_digits=14, decimal_places=2)
    total_revenue = models.DecimalField(max_digits=16, decimal_places=2)

    class Meta:
        db_table = "ga4_daily"
        constraints = [
            models.UniqueConstraint(
                fields=["client", "date", "session_source", "session_medium", "device_category"],
                name="ga4_daily_uniq",
            )
        ]


class AdsCampaignDaily(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    date = models.DateField()
    campaign_id = models.BigIntegerField()
    campaign_name = models.CharField(max_length=255)
    campaign_status = models.CharField(max_length=32)
    channel_type = models.CharField(max_length=32)
    impressions = models.BigIntegerField()
    clicks = models.BigIntegerField()
    # numeric, not float: Postgres ROUND(x, 2) only accepts numeric, a common LLM SQL failure otherwise
    cost = models.DecimalField(max_digits=14, decimal_places=2, help_text="Account currency (cost_micros / 1e6)")
    conversions = models.DecimalField(max_digits=14, decimal_places=2)
    conversions_value = models.DecimalField(max_digits=16, decimal_places=2)

    class Meta:
        db_table = "ads_campaign_daily"
        constraints = [
            models.UniqueConstraint(fields=["client", "date", "campaign_id"], name="ads_campaign_daily_uniq")
        ]
