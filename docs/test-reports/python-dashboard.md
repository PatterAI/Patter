# Python Dashboard — Test Report

**Run at:** 2026-04-12T03:22:28Z
**Working dir:** /Users/francescorosciano/docs/patter/Patter/sdk
**Command:** `python3 -m pytest tests/test_dashboard*.py -v --tb=short`
**Runtime:** 0.46s

## Summary
- Total: 34
- Passed: 34
- Failed: 0
- Skipped: 0
- Errors: 0
- Exit code: 0

## Failures
None.

## Full output (tail)
```
tests/test_dashboard.py::TestMetricsStore::test_empty_store PASSED       [  2%]
tests/test_dashboard.py::TestMetricsStore::test_record_call_start PASSED [  5%]
tests/test_dashboard.py::TestMetricsStore::test_record_call_end PASSED   [  8%]
tests/test_dashboard.py::TestMetricsStore::test_get_call_by_id PASSED    [ 11%]
tests/test_dashboard.py::TestMetricsStore::test_calls_ordered_newest_first PASSED [ 14%]
tests/test_dashboard.py::TestMetricsStore::test_max_calls_limit PASSED   [ 17%]
tests/test_dashboard.py::TestMetricsStore::test_pagination PASSED        [ 20%]
tests/test_dashboard.py::TestMetricsStore::test_record_turn PASSED       [ 23%]
tests/test_dashboard.py::TestMetricsStore::test_aggregates_empty PASSED  [ 26%]
tests/test_dashboard.py::TestMetricsStore::test_aggregates_with_calls PASSED [ 29%]
tests/test_dashboard.py::TestMetricsStore::test_active_calls_count_in_aggregates PASSED [ 32%]
tests/test_dashboard.py::TestMetricsStore::test_record_call_start_ignores_empty_id PASSED [ 35%]
tests/test_dashboard.py::TestMetricsStore::test_record_call_end_without_start PASSED [ 38%]
tests/test_dashboard.py::TestDashboardRoutes::test_mount_dashboard_adds_routes PASSED [ 41%]
tests/test_dashboard.py::TestDashboardHTML::test_html_contains_key_elements PASSED [ 44%]
tests/test_dashboard.py::TestServerDashboardIntegration::test_server_has_dashboard_param PASSED [ 47%]
tests/test_dashboard.py::TestServerDashboardIntegration::test_serve_has_dashboard_param PASSED [ 50%]
tests/test_dashboard.py::TestServerDashboardIntegration::test_wrap_callbacks_returns_callables PASSED [ 52%]
tests/test_dashboard_auth.py::test_auth_valid_header PASSED              [ 55%]
tests/test_dashboard_auth.py::test_auth_valid_query_param PASSED         [ 58%]
tests/test_dashboard_auth.py::test_auth_invalid_token PASSED             [ 61%]
tests/test_dashboard_auth.py::test_auth_missing_token PASSED             [ 64%]
tests/test_dashboard_auth.py::test_no_auth_allows_all PASSED             [ 67%]
tests/test_dashboard_export.py::test_csv_output PASSED                   [ 70%]
tests/test_dashboard_export.py::test_csv_empty PASSED                    [ 73%]
tests/test_dashboard_export.py::test_json_output PASSED                  [ 76%]
tests/test_dashboard_export.py::test_json_empty PASSED                   [ 79%]
tests/test_dashboard_export.py::test_export_endpoint_csv PASSED          [ 82%]
tests/test_dashboard_sse.py::test_subscribe_and_receive_event PASSED     [ 88%]
tests/test_dashboard_sse.py::test_turn_complete_event PASSED             [ 91%]
tests/test_dashboard_sse.py::test_call_end_event PASSED                  [ 94%]
tests/test_dashboard_sse.py::test_unsubscribe PASSED                     [ 97%]
tests/test_dashboard_sse.py::test_full_queue_drops_subscriber PASSED     [100%]

======================= 34 passed, 109 warnings in 0.46s =======================
```
