"""Quota management tests — file I/O, month rollover, header parsing."""

import json
import threading
from unittest.mock import patch

from pivot_web_search_mcp import quota


class TestLoadQuota:
    def test_empty_for_new_file(self):
        data = quota.load_quota()
        assert data == {}

    def test_reads_existing(self, tmp_path):
        quota._QUOTA_FILE.write_text(json.dumps({
            "tavily": {"month": quota._current_month(), "used": 42, "limit": 1000, "source": "api"}
        }))
        data = quota.load_quota()
        assert data["tavily"]["used"] == 42

    def test_month_rollover_resets(self, tmp_path):
        quota._QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
        quota._QUOTA_FILE.write_text(json.dumps({
            "tavily": {"month": "2020-01", "used": 999, "limit": 1000, "source": "api"}
        }))
        data = quota.load_quota()
        assert data["tavily"]["used"] == 0
        assert data["tavily"]["month"] == quota._current_month()


class TestRecordUsage:
    def test_increments_count(self):
        quota.record_usage("tavily")
        quota.record_usage("tavily")
        data = quota.load_quota()
        assert data["tavily"]["used"] == 2

    def test_ddg_not_tracked(self):
        quota.record_usage("ddg")
        quota.record_usage("DDG")
        data = quota.load_quota()
        assert "ddg" not in data
        assert "DDG" not in data

    def test_persists_to_disk(self):
        quota.record_usage("brave")
        raw = json.loads(quota._QUOTA_FILE.read_text())
        assert raw["brave"]["used"] == 1


class TestBraveHeaders:
    def test_parses_monthly_values(self):
        headers = {
            "X-RateLimit-Remaining": "1, 14500",
            "X-RateLimit-Limit": "1, 15000",
        }
        quota.update_from_brave_headers(headers)
        data = quota.load_quota()
        assert data["brave"]["remaining"] == 14500
        assert data["brave"]["limit"] == 15000
        assert data["brave"]["used"] == 500
        assert data["brave"]["source"] == "header"
        assert data["brave"]["period"] == "rolling"

    def test_parses_reset_header(self):
        headers = {
            "X-RateLimit-Remaining": "1, 14000",
            "X-RateLimit-Limit": "1, 15000",
            "X-RateLimit-Reset": "1, 1209600",
        }
        quota.update_from_brave_headers(headers)
        data = quota.load_quota()
        assert data["brave"]["reset_at"] is not None
        from datetime import datetime
        reset_dt = datetime.fromisoformat(data["brave"]["reset_at"])
        assert reset_dt > datetime.now(quota.timezone.utc)

    def test_ignores_missing_headers(self):
        quota.update_from_brave_headers({})
        data = quota.load_quota()
        assert "brave" not in data

    def test_ignores_single_value(self):
        headers = {"X-RateLimit-Remaining": "5", "X-RateLimit-Limit": "10"}
        quota.update_from_brave_headers(headers)
        data = quota.load_quota()
        assert "brave" not in data


class TestRollingReset:
    def test_rolling_resets_after_reset_at(self):
        from datetime import datetime, timedelta
        past = (datetime.now(quota.timezone.utc) - timedelta(hours=1)).isoformat()
        quota._QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
        quota._QUOTA_FILE.write_text(json.dumps({
            "brave": {
                "period": "rolling",
                "used": 1000,
                "limit": 15000,
                "remaining": 14000,
                "source": "header",
                "reset_at": past,
            }
        }))
        quota._quota_cache = None
        data = quota.load_quota()
        assert data["brave"]["used"] == 0
        assert data["brave"]["period"] == "rolling"

    def test_rolling_does_not_reset_before_reset_at(self):
        from datetime import datetime, timedelta
        future = (datetime.now(quota.timezone.utc) + timedelta(days=14)).isoformat()
        quota._QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
        quota._QUOTA_FILE.write_text(json.dumps({
            "brave": {
                "period": "rolling",
                "used": 5000,
                "limit": 15000,
                "remaining": 10000,
                "source": "header",
                "reset_at": future,
            }
        }))
        quota._quota_cache = None
        data = quota.load_quota()
        assert data["brave"]["used"] == 5000

    def test_rolling_no_reset_at_stays_unchanged(self):
        quota._QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
        quota._QUOTA_FILE.write_text(json.dumps({
            "brave": {
                "period": "rolling",
                "used": 3000,
                "limit": 15000,
                "source": "header",
                "reset_at": None,
            }
        }))
        quota._quota_cache = None
        data = quota.load_quota()
        assert data["brave"]["used"] == 3000


class TestGetUsagePct:
    def test_correct_percentage(self):
        quota.record_usage("tavily")
        quota.set_provider_limit("tavily", 100)
        pct = quota.get_usage_pct("tavily")
        assert pct == 1.0

    def test_zero_when_no_limit(self):
        quota.record_usage("tavily")
        pct = quota.get_usage_pct("tavily")
        assert pct == 0.0

    def test_unknown_provider(self):
        pct = quota.get_usage_pct("nonexistent")
        assert pct == 0.0


class TestIsExhausted:
    def test_true_at_limit(self):
        quota.set_provider_limit("tavily", 2)
        quota.record_usage("tavily")
        quota.record_usage("tavily")
        assert quota.is_exhausted("tavily") is True

    def test_false_below_limit(self):
        quota.set_provider_limit("tavily", 100)
        quota.record_usage("tavily")
        assert quota.is_exhausted("tavily") is False

    def test_false_no_limit(self):
        quota.record_usage("tavily")
        assert quota.is_exhausted("tavily") is False


class TestConcurrentWrites:
    def test_no_corruption(self):
        quota.set_provider_limit("test_prov", 10000)
        errors = []

        def _writer(n):
            try:
                for _ in range(50):
                    quota.record_usage("test_prov")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        data = quota.load_quota()
        assert data["test_prov"]["used"] == 200


class TestGetQuotaSummary:
    def test_returns_tracked_providers(self):
        quota.set_provider_limit("tavily", 1000)
        quota.record_usage("tavily")
        summary = quota.get_quota_summary()
        assert "tavily" in summary
        assert summary["tavily"]["used"] == 1
        assert summary["tavily"]["limit"] == 1000
        assert summary["tavily"]["usage_pct"] == 0.1


class TestDailyReset:
    def test_daily_provider_resets_on_new_day(self):
        quota.set_provider_limit("gemini", 500, period="daily")
        quota.record_usage("gemini")
        quota.record_usage("gemini")
        data = quota.load_quota()
        assert data["gemini"]["used"] == 2
        assert data["gemini"]["period"] == "daily"

        with patch.object(quota, "_current_day_pt", return_value="2099-12-31"):
            quota._quota_cache = None
            data = quota.load_quota()
            assert data["gemini"]["used"] == 0
            assert data["gemini"]["day"] == "2099-12-31"

    def test_daily_provider_preserves_limit_on_reset(self):
        quota.set_provider_limit("gemini", 100, period="daily")
        quota.record_usage("gemini")

        with patch.object(quota, "_current_day_pt", return_value="2099-01-01"):
            quota._quota_cache = None
            data = quota.load_quota()
            assert data["gemini"]["limit"] == 100
            assert data["gemini"]["used"] == 0

    def test_daily_exhausted_resets_next_day(self):
        quota.set_provider_limit("gemini", 2, period="daily")
        quota.record_usage("gemini")
        quota.record_usage("gemini")
        assert quota.is_exhausted("gemini") is True

        with patch.object(quota, "_current_day_pt", return_value="2099-06-15"):
            quota._quota_cache = None
            assert quota.is_exhausted("gemini") is False

    def test_monthly_provider_unaffected_by_daily_logic(self):
        quota.set_provider_limit("tavily", 1000)
        quota.record_usage("tavily")
        data = quota.load_quota()
        assert "period" not in data["tavily"]
        assert data["tavily"]["used"] == 1

    def test_record_usage_respects_daily_period(self):
        quota.set_provider_limit("gemini", 500, period="daily")
        quota.record_usage("gemini")

        with patch.object(quota, "_current_day_pt", return_value="2099-03-20"):
            quota.record_usage("gemini")
            data = quota.load_quota()
            assert data["gemini"]["used"] == 1
            assert data["gemini"]["day"] == "2099-03-20"
