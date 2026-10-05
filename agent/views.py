import psycopg
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.views.generic import TemplateView
from rest_framework import serializers
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .core.chains import generate_sql, select_examples, summarize
from .core.executor import run_sql
from .core.guards import SqlRejected, validate_sql
from .core.llm import embed
from .models import CacheEntry, QueryLog
from .services import ask, user_clients


class AppView(LoginRequiredMixin, TemplateView):
    template_name = "app.html"


def healthz(request):
    return JsonResponse({"ok": True})


class AskIn(serializers.Serializer):
    client_id = serializers.IntegerField()
    question = serializers.CharField(min_length=3, max_length=500)


class AskView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "ask"

    def post(self, request):
        data = AskIn(data=request.data)
        data.is_valid(raise_exception=True)
        payload, status = ask(request.user, **data.validated_data)
        return Response(payload, status=status)


class ClientsView(APIView):
    def get(self, request):
        return Response([
            {"id": c.id, "name": c.name, "slug": c.slug, "currency": c.currency_code, "last_synced_at": c.last_synced_at}
            for c in user_clients(request.user).order_by("name")
        ])


class HistoryView(APIView):
    def get(self, request):
        logs = QueryLog.objects.filter(user=request.user).order_by("-created_at")
        if request.query_params.get("client_id"):
            logs = logs.filter(client_id=request.query_params["client_id"])
        return Response(list(logs.values("id", "question", "status", "cache_hit", "feedback", "created_at")[:20]))


class FeedbackIn(serializers.Serializer):
    query_id = serializers.IntegerField()
    rating = serializers.ChoiceField(choices=[-1, 1])


class FeedbackView(APIView):
    def post(self, request):
        data = FeedbackIn(data=request.data)
        data.is_valid(raise_exception=True)
        log = QueryLog.objects.filter(pk=data.validated_data["query_id"], user=request.user).first()
        if not log:
            return Response({"ok": False}, status=404)
        log.feedback = data.validated_data["rating"]
        log.save(update_fields=["feedback"])
        if log.feedback == -1 and log.sql:  # evict exact AND semantic copies of the answer the user rejected
            CacheEntry.objects.filter(client_id=log.client_id, response__sql=log.sql).delete()
        return Response({"ok": True})


# ---------- staff-only debug endpoints: one per pipeline step, for Postman ----------

class QuestionIn(serializers.Serializer):
    question = serializers.CharField(max_length=500)


class SqlIn(serializers.Serializer):
    sql = serializers.CharField()


class RunSqlIn(SqlIn):
    client_id = serializers.IntegerField()


class SummarizeIn(QuestionIn):
    sql = serializers.CharField()
    columns = serializers.ListField()
    rows = serializers.ListField()


class DebugView(APIView):
    permission_classes = [IsAdminUser]
    serializer = QuestionIn

    def post(self, request):
        data = self.serializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(self.run(**data.validated_data))


class DebugExamples(DebugView):
    def run(self, question):
        return {"examples": select_examples(embed(question)[0])}


class DebugGenerateSql(DebugView):
    def run(self, question):
        draft, usage = generate_sql(question, select_examples(embed(question)[0]))
        return {**draft.model_dump(), "usage": usage}


class DebugValidateSql(DebugView):
    serializer = SqlIn

    def post(self, request):
        try:
            return super().post(request)
        except SqlRejected as e:
            return Response({"ok": False, "reason": str(e)}, status=422)

    def run(self, sql):
        return {"ok": True, "safe_sql": validate_sql(sql)}


class DebugRunSql(DebugValidateSql):
    serializer = RunSqlIn

    def run(self, sql, client_id):
        if not user_clients(self.request.user).filter(pk=client_id).exists():
            return {"error": "unknown client"}
        safe = validate_sql(sql)  # never run unvalidated SQL, even for staff
        try:
            return {"safe_sql": safe, **run_sql(safe, client_id)}
        except psycopg.Error as e:
            return {"safe_sql": safe, "error": str(e).strip().splitlines()[0]}


class DebugSummarize(DebugView):
    serializer = SummarizeIn

    def run(self, question, sql, columns, rows):
        text, usage = summarize(question, sql, columns, rows)
        return {"answer": text, "usage": usage}
