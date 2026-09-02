import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
DOCUMENTS = (
    ROOT / "docs" / "2026-08-31-エージェント艦隊-作業進行ルール.md",
    ROOT / "docs" / "2026-08-31-エージェント艦隊-連携と全体制御.md",
    ROOT / "docs" / "2026-08-31-エージェント艦隊-非機能要件.md",
    ROOT / "docs" / "2026-09-01-エージェント艦隊-役割Hookの実行契機と責務.md",
)
CORE_TESTS = ROOT / "plugins" / "agent-fleet-core" / "core" / "tests" / "test_fleet_control.py"
RUNTIME_TESTS = ROOT / "plugins" / "agent-fleet-herdr" / "adapter" / "tests" / "test_fleet_runtime.py"
ADAPTER_TESTS = ROOT / "plugins" / "agent-fleet-herdr" / "adapter" / "tests" / "test_herdr_adapter.py"
HOOK_TESTS = ROOT / "plugins" / "agent-fleet-herdr" / "adapter" / "tests" / "test_role_context_hook.py"
CONTROLLER_TESTS = ROOT / "plugins" / "agent-fleet-herdr" / "adapter" / "tests" / "test_fleet_controller.py"
BENCHMARK = ROOT / "scripts" / "benchmark_nfr.py"


COVERAGE = {
    "マネージャーが未着手タスクの担当を決める": (
        (CORE_TESTS, "test_deterministic_assignment_and_outbox_commands_are_idempotent"),
    ),
    "作業者は他者へタスクを割り当てられない": (
        (CORE_TESTS, "test_non_manager_cannot_assign_and_pending_task_is_unchanged"),
    ),
    "作業者の節目報告をマネージャーが確認できる": (
        (CORE_TESTS, "test_progress_report_is_idempotent_and_notifies_manager_atomically"),
    ),
    "担当ではない作業者の報告でタスク状態を変えない": (
        (CORE_TESTS, "test_only_assignee_can_report_task_state"),
    ),
    "マネージャーが各役割の報告から全体進捗を要約する": (
        (CORE_TESTS, "test_terminal_worker_report_enqueues_manager_review_command"),
        (CORE_TESTS, "test_status_exposes_pending_command_as_adapter_request_json"),
    ),
    "作業者の完了報告だけではタスクを受容しない": (
        (CORE_TESTS, "test_completion_report_requires_manager_acceptance"),
    ),
    "同じ節目報告を二重に数えない": (
        (CORE_TESTS, "test_progress_report_is_idempotent_and_notifies_manager_atomically"),
    ),
    "報告予定を二回続けて過ぎたタスクを利用者判断へ上げる": (
        (CORE_TESTS, "test_two_consecutive_missed_deadlines_require_user_decision"),
        (CORE_TESTS, "test_deadline_boundary_and_terminal_task_do_not_escalate"),
    ),
    "一回目の報告予定超過では担当者へ状況確認を求める": (
        (CORE_TESTS, "test_first_missed_report_deadline_requests_a_status_check_once"),
    ),
    "次回報告予定と同じ時刻の確認は期限超過にしない": (
        (CORE_TESTS, "test_deadline_boundary_and_terminal_task_do_not_escalate"),
    ),
    "期限内の報告によって連続超過を解消する": (
        (CORE_TESTS, "test_on_time_report_clears_a_previous_missed_deadline"),
    ),
    "マネージャー不在中の報告を復帰後に確認する": (
        (CORE_TESTS, "test_manager_can_read_a_report_saved_while_no_delivery_worker_runs"),
    ),
    "マネージャーがCoreの公開操作から進捗を判断する": (
        (CORE_TESTS, "test_task_list_exposes_a_compact_manager_monitoring_view"),
        (CORE_TESTS, "test_task_list_keeps_the_terminal_report_visible_after_acceptance"),
        (CORE_TESTS, "test_task_list_cli_does_not_require_external_json_processing"),
        (CORE_TESTS, "test_later_context_sync_preserves_manager_monitoring_control"),
    ),
    "マネージャーだけが完了報告済みタスクを受容する": (
        (CORE_TESTS, "test_completion_report_requires_manager_acceptance"),
    ),
    "差し戻されたタスクを担当者が再開する": (
        (CORE_TESTS, "test_reported_task_can_be_returned_to_the_assignee_but_failed_task_stays_terminal"),
        (CORE_TESTS, "test_resuming_after_block_or_rework_does_not_enqueue_a_second_context_sync"),
    ),
    "作業開始報告で進行中の仕事へ役割同期を割り込ませない": (
        (CORE_TESTS, "test_running_report_does_not_interrupt_the_agent_with_context_sync"),
        (CORE_TESTS, "test_current_session_context_rejects_stale_or_unbound_sessions"),
    ),
    "停止後に再開する仕事へ役割同期を割り込ませない": (
        (CORE_TESTS, "test_resuming_after_block_or_rework_does_not_enqueue_a_second_context_sync"),
    ),
    "依存する仕事は前のタスクが受容された後に始められる": (
        (CORE_TESTS, "test_accepting_task_releases_newly_unblocked_dependents"),
    ),
    "失敗したタスクを自動で再開しない": (
        (CORE_TESTS, "test_reported_task_can_be_returned_to_the_assignee_but_failed_task_stays_terminal"),
    ),
    "三回目の失敗後は利用者判断なしに再試行しない": (
        (HOOK_TESTS, "test_role_context_includes_role_specific_duties"),
    ),
    "独立確認が必要な成果を確認結果なしで受容しない": (
        (HOOK_TESTS, "test_role_context_includes_role_specific_duties"),
    ),
    "現在の役割文脈を確認していないメンバーへ新しい仕事を開始させない": (
        (CORE_TESTS, "test_work_command_waits_until_current_role_context_is_confirmed"),
    ),
    "起動済み艦隊がプラグイン更新後も同じHookで作業を続ける": (
        (RUNTIME_TESTS, "test_active_fleet_keeps_its_hook_snapshot_until_stop_and_restart"),
    ),
    "停止後の再起動で新しいHookへ切り替える": (
        (RUNTIME_TESTS, "test_active_fleet_keeps_its_hook_snapshot_until_stop_and_restart"),
    ),
    "固定版が変更されている艦隊を再開しない": (
        (RUNTIME_TESTS, "test_start_rejects_a_modified_materialized_hook_runtime"),
    ),
    "CodexのHook確認だけを事前承認して艦隊を起動する": (
        (ADAPTER_TESTS, "test_codex_fleet_preapproves_hook_trust_without_bypassing_other_approvals"),
    ),
    "役割文脈を確認できない会話へ通常指示を届けない": (
        (CORE_TESTS, "test_work_command_waits_until_current_role_context_is_confirmed"),
    ),
    "同じ起動済み艦隊を再指定しても作業枠を増やさない": (
        (RUNTIME_TESTS, "test_start_provisions_context_and_tasks_then_runs_paneless_controller"),
    ),
    "起動中の艦隊へ異なる編成を暗黙適用しない": (
        (ADAPTER_TESTS, "test_provision_rejects_existing_fleet_with_different_profile"),
    ),
    "二つの配送実行役が同じ指示を取得しない": (
        (CORE_TESTS, "test_two_delivery_workers_cannot_claim_the_same_command"),
    ),
    "送信結果不明の指示を自動で再送しない": (
        (CORE_TESTS, "test_delivery_claim_has_a_fenced_lease_and_unknown_is_not_retried"),
    ),
    "遅れて届いたHook受領で結果不明を配送済みへ訂正する": (
        (CORE_TESTS, "test_late_hook_receipt_corrects_unknown_delivery"),
    ),
    "同じ論理識別子を持つ複数艦隊を混同しない": (
        (ADAPTER_TESTS, "test_same_agent_ref_in_two_fleets_resolves_to_each_fleets_pane"),
    ),
    "消失した作業枠へ別の作業枠を自動対応付けしない": (
        (ADAPTER_TESTS, "test_missing_pane_is_detected_and_requires_rebind"),
    ),
    "利用者設定だけから艦隊と表示配置を解決する": (
        (RUNTIME_TESTS, "test_cli_defaults_only_to_user_configuration_directories"),
    ),
    "dry-runでは艦隊状態と作業枠を変更しない": (
        (ADAPTER_TESTS, "test_provision_dry_run_does_not_create_requested_state_database"),
    ),
    "再開した艦隊会話へ現在の役割文脈を戻す": (
        (HOOK_TESTS, "test_fleet_prompt_binds_session_and_compaction_restores_context"),
    ),
    "艦隊外の会話では通常promptへ介入しない": (
        (HOOK_TESTS, "test_unrelated_prompt_and_unknown_session_receive_no_fleet_context"),
    ),
    "艦隊agent sessionだけで役割Hookを有効にする": (
        (ADAPTER_TESTS, "test_fleet_agents_enable_session_only_hook_plugin"),
    ),
    "担当変更後の通常promptへ新しい役割文脈を追加する": (
        (HOOK_TESTS, "test_active_session_refreshes_latest_context_on_every_ordinary_prompt"),
    ),
    "Coreと一致しない艦隊指示をモデルへ渡さない": (
        (HOOK_TESTS, "test_forged_non_context_command_is_blocked_when_core_rejects_it"),
    ),
    "Claude Codeの分岐会話へ元の担当を引き継がない": (
        (HOOK_TESTS, "test_claude_fork_gets_a_durable_unbound_state_under_its_new_session_id"),
    ),
    "未関連の会話を再開しても艦隊の役割を注入しない": (
        (HOOK_TESTS, "test_unrelated_prompt_and_unknown_session_receive_no_fleet_context"),
    ),
    "Coreを確認できない艦隊会話の通常promptを拒否する": (
        (HOOK_TESTS, "test_active_session_blocks_ordinary_prompt_when_context_cannot_be_read"),
    ),
    "同じ会話識別子を製品間で混同しない": (
        (HOOK_TESTS, "test_same_session_id_is_isolated_by_runtime_product"),
    ),
    "同じ指示を同じ会話へ二度提示しない": (
        (HOOK_TESTS, "test_same_command_is_not_presented_twice_to_one_session"),
    ),
    "Core確定後に応答だけ失った受領を一度再照合する": (
        (HOOK_TESTS, "test_consume_retries_signal_exit_or_broken_json_after_core_commit"),
    ),
    "役割ごとの行動境界を現在文脈へ含める": (
        (HOOK_TESTS, "test_role_context_includes_role_specific_duties"),
    ),
    "役割同期だけでは担当作業を開始しない": (
        (HOOK_TESTS, "test_role_context_includes_role_specific_duties"),
    ),
    "マネージャーへ公開された監視方法を役割文脈へ含める": (
        (RUNTIME_TESTS, "test_start_provisions_context_and_tasks_then_runs_paneless_controller"),
        (HOOK_TESTS, "test_role_context_includes_role_specific_duties"),
    ),
    "マネージャー以外へ艦隊全体の監視方法を渡さない": (
        (RUNTIME_TESTS, "test_start_provisions_context_and_tasks_then_runs_paneless_controller"),
    ),
    "再起動前の会話を新しい起動世代で使わない": (
        (CORE_TESTS, "test_new_runtime_confirmation_does_not_reactivate_old_session"),
    ),
    "改ざんされた艦隊有効化指示を拒否する": (
        (HOOK_TESTS, "test_malformed_or_invalid_fleet_activation_prompt_is_blocked"),
    ),
    "配送対象が続く間は待機を挟まない": (
        (RUNTIME_TESTS, "test_monitor_backs_off_only_while_idle_and_resets_after_delivery"),
    ),
    "配送対象がない間だけ待機時間を段階的に伸ばす": (
        (RUNTIME_TESTS, "test_monitor_backs_off_only_while_idle_and_resets_after_delivery"),
    ),
    "配送後は次の空振り待機を250ミリ秒へ戻す": (
        (RUNTIME_TESTS, "test_monitor_backs_off_only_while_idle_and_resets_after_delivery"),
    ),
    "二つの配送実行役でも一つの指示は一度だけ確保される": (
        (CORE_TESTS, "test_two_delivery_workers_cannot_claim_the_same_command"),
    ),
    "送信開始前に失効した確保を別の配送実行役が回収する": (
        (CORE_TESTS, "test_expired_delivery_lease_is_reclaimed_and_old_token_is_fenced"),
    ),
    "送信開始後に結果を失った指示を再送しない": (
        (CORE_TESTS, "test_delivery_started_before_external_send_is_not_retried_after_lease_expiry"),
    ),
    "制御処理の停止後も艦隊の作業枠と確定状態を残す": (
        (RUNTIME_TESTS, "test_start_provisions_context_and_tasks_then_runs_paneless_controller"),
    ),
    "同じ起動コマンドで未配送分から再開する": (
        (RUNTIME_TESTS, "test_start_provisions_context_and_tasks_then_runs_paneless_controller"),
    ),
    "三艦隊を同時処理しても指示を混入させない": (
        (CORE_TESTS, "test_two_fleets_may_reuse_logical_ids_without_cross_talk"),
        (BENCHMARK, "run"),
    ),
    "状態確認は管理対象を変更しない": (
        (ADAPTER_TESTS, "test_status_is_read_only_and_reports_profile_bindings_and_placements"),
    ),
    "期限確認の一時失敗でも保存済み指示の配送を止めない": (
        (CONTROLLER_TESTS, "test_deadline_check_failure_does_not_starve_existing_delivery_work"),
    ),
    "標準負荷で報告通知と状態確認の目標を判定する": (
        (BENCHMARK, "run"),
    ),
}


def documented_scenarios() -> dict[str, str]:
    scenarios = {}
    for document in DOCUMENTS:
        text = document.read_text(encoding="utf-8")
        for block in re.findall(r"```gherkin\n(.*?)\n```", text, flags=re.DOTALL):
            title_match = re.search(r"^(?:Scenario|シナリオ):\s*(.+)$", block, re.MULTILINE)
            if title_match is None:
                raise AssertionError(f"Gherkin block without a scenario title: {document}")
            title = title_match.group(1).strip()
            if title in scenarios:
                raise AssertionError(f"duplicate documented scenario: {title}")
            scenarios[title] = block
    return scenarios


class DocumentedBehaviorTraceabilityTest(unittest.TestCase):
    def test_every_documented_scenario_has_given_when_then_in_one_block(self):
        scenarios = documented_scenarios()
        self.assertGreaterEqual(len(scenarios), 50)
        for title, block in scenarios.items():
            with self.subTest(scenario=title):
                self.assertRegex(block, r"(?m)^\s*(?:Given|前提)\s+")
                self.assertRegex(block, r"(?m)^\s*(?:When|もし)\s+")
                self.assertRegex(block, r"(?m)^\s*(?:Then|なら)\s+")

    def test_every_documented_scenario_has_executable_evidence(self):
        scenarios = documented_scenarios()
        self.assertEqual(set(scenarios), set(COVERAGE))
        source_cache = {}
        for title, evidence in COVERAGE.items():
            self.assertTrue(evidence, title)
            for source, test_name in evidence:
                text = source_cache.setdefault(source, source.read_text(encoding="utf-8"))
                with self.subTest(scenario=title, test=test_name):
                    self.assertIn(f"def {test_name}(", text)


if __name__ == "__main__":
    unittest.main()
