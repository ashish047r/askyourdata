from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path
from rest_framework.authtoken.views import obtain_auth_token

from agent import views as v

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("healthz", v.healthz),
    path("api/auth/token/", obtain_auth_token),
    path("api/clients/", v.ClientsView.as_view()),
    path("api/ask/", v.AskView.as_view()),
    path("api/history/", v.HistoryView.as_view()),
    path("api/feedback/", v.FeedbackView.as_view()),
    path("api/debug/examples/", v.DebugExamples.as_view()),
    path("api/debug/generate-sql/", v.DebugGenerateSql.as_view()),
    path("api/debug/validate-sql/", v.DebugValidateSql.as_view()),
    path("api/debug/run-sql/", v.DebugRunSql.as_view()),
    path("api/debug/summarize/", v.DebugSummarize.as_view()),
    path("", v.AppView.as_view(), name="app"),
]
