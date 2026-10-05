from django.contrib import admin

from .models import AdsCampaignDaily, Client, Ga4Daily


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "ga4_property_id", "ads_customer_id", "currency_code", "last_synced_at", "is_active")
    filter_horizontal = ("users",)
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Ga4Daily)
class Ga4DailyAdmin(admin.ModelAdmin):
    list_display = ("client", "date", "session_source", "session_medium", "device_category", "sessions", "key_events")
    list_filter = ("client", "device_category")
    date_hierarchy = "date"


@admin.register(AdsCampaignDaily)
class AdsCampaignDailyAdmin(admin.ModelAdmin):
    list_display = ("client", "date", "campaign_name", "channel_type", "clicks", "cost", "conversions")
    list_filter = ("client", "channel_type")
    date_hierarchy = "date"
