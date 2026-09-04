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
SPEC_TESTS = ROOT / "plugins" / "agent-fleet-core" / "spec" / "tests" / "test_validate_fleet.py"
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
    "マネージャーが未受理の依存タスクを手動で割り当てようとする": (
        (CORE_TESTS, "test_manual_assignment_rejects_pending_and_reported_dependencies"),
        (CORE_TESTS, "test_acceptance_releases_multiple_dependencies_and_assignment_retry_is_idempotent"),
    ),
    "マネージャーが状態一覧だけで依存関係を監視する": (
        (CORE_TESTS, "test_task_list_includes_declared_dependencies_in_order_after_acceptance"),
    ),
    "失敗したタスクを自動で再開しない": (
        (CORE_TESTS, "test_reported_task_can_be_returned_to_the_assignee_but_failed_task_stays_terminal"),
    ),
    "現在の役割文脈を確認していないメンバーへ新しい仕事を開始させない": (
        (CORE_TESTS, "test_work_command_waits_until_current_role_context_is_confirmed"),
    ),
    "起動済み艦隊は導入元のHook更新へ暗黙追従しない": (
        (RUNTIME_TESTS, "test_active_resume_keeps_saved_hook_and_stopped_restart_uses_new_hook"),
    ),
    "停止後の再起動で新しいHook固定版へ切り替える": (
        (RUNTIME_TESTS, "test_active_resume_keeps_saved_hook_and_stopped_restart_uses_new_hook"),
    ),
    "異なるHookを新しいfleet IDで起動する": (
        (RUNTIME_TESTS, "test_different_hook_can_start_with_new_fleet_id"),
    ),
    "固定版が変更されている艦隊を再開しない": (
        (RUNTIME_TESTS, "test_start_rejects_a_modified_materialized_hook_runtime"),
    ),
    "CodexのHook確認だけを事前承認して艦隊を起動する": (
        (ADAPTER_TESTS, "test_codex_fleet_preapproves_hook_trust_without_bypassing_other_approvals"),
    ),
    "Codexの役割Hook登録がない間はCodexの作業枠を作らない": (
        (RUNTIME_TESTS, "test_start_rejects_missing_codex_hook_registration_before_state_creation"),
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
    "Hook受領と送信結果が同時でも配送済みから戻さない": (
        (CORE_TESTS, "test_concurrent_hook_receipt_cannot_be_overwritten_by_unknown_result"),
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
    "一つの艦隊でCodexとClaudeをメンバーごとに使い分ける": (
        (ADAPTER_TESTS, "test_member_runtime_can_mix_claude_fable_and_codex_sol"),
    ),
    "Claude Fableを古いモデルへ暗黙に切り替えない": (
        (ADAPTER_TESTS, "test_member_runtime_can_mix_claude_fable_and_codex_sol"),
    ),
    "AI実行設定がないメンバーを含む艦隊を起動しない": (
        (SPEC_TESTS, "test_member_runtime_is_required_and_legacy_top_level_model_is_rejected"),
    ),
    "Herdrを参照しない艦隊設定を別の実行基盤でも再利用できる": (
        (SPEC_TESTS, "test_fleet_rejects_herdr_runtime_and_view_configuration"),
    ),
    "Herdr起動設定が艦隊と表示プロファイルを一方向に合成する": (
        (RUNTIME_TESTS, "test_launch_profile_composes_fleet_and_versioned_view_profile"),
    ),
    "三つの役割群を三列へ配置する": (
        (ADAPTER_TESTS, "test_role_groups_compile_to_three_columns_with_equal_vertical_stacks"),
    ),
    "起動名と艦隊IDが異なっても論理状態と実行状態を混同しない": (
        (RUNTIME_TESTS, "test_launch_identity_may_differ_from_fleet_identity_without_mixing_state_paths"),
    ),
    "同じ艦隊を別の起動設定から同時に起動しない": (
        (RUNTIME_TESTS, "test_second_launch_profile_for_active_fleet_is_rejected_before_new_state"),
        (RUNTIME_TESTS, "test_same_launch_is_locked_even_when_its_fleet_reference_changes"),
    ),
    "起動設定と参照先が一致しない間は状態を変えない": (
        (ADAPTER_TESTS, "test_provision_rejects_launch_reference_mismatch_before_herdr_calls"),
        (ADAPTER_TESTS, "test_provision_cli_rejects_invalid_composition_before_state_creation"),
    ),
    "表示条件がメンバーを重複または未割当にする間は起動しない": (
        (ADAPTER_TESTS, "test_role_groups_reject_duplicate_and_unassigned_members"),
    ),
    "起動済み配置へ同じ版名の異なる表示内容を適用しない": (
        (ADAPTER_TESTS, "test_provision_rejects_changed_view_content_with_same_profile_identity"),
    ),
    "旧Fleet v1を暗黙に新しい起動設定として扱わない": (
        (RUNTIME_TESTS, "test_legacy_fleet_requires_explicit_compatibility_switch"),
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
        (HOOK_TESTS, "test_role_context_uses_resolved_catalog_definition"),
    ),
    "Role Catalogに存在しない役割参照で艦隊を起動しない": (
        (SPEC_TESTS, "test_catalog_rejects_missing_role_ref"),
    ),
    "起動時に固定したRole Catalogの変更を構成差分として検出する": (
        (RUNTIME_TESTS, "test_status_detects_role_catalog_drift"),
    ),
    "役割同期だけでは担当作業を開始しない": (
        (HOOK_TESTS, "test_role_context_uses_resolved_catalog_definition"),
    ),
    "マネージャーへ公開された監視方法を役割文脈へ含める": (
        (RUNTIME_TESTS, "test_start_provisions_context_and_tasks_then_runs_paneless_controller"),
        (HOOK_TESTS, "test_role_context_uses_resolved_catalog_definition"),
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
    "起動前の検査中に設定が変わった場合は状態を作らない": (
        (RUNTIME_TESTS, "test_resolve_rejects_fleet_changed_while_core_validates_it"),
        (RUNTIME_TESTS, "test_start_rejects_config_changed_during_preflight_before_state_creation"),
    ),
    "真偽値を表示プロファイルの版番号として扱わない": (
        (RUNTIME_TESTS, "test_boolean_view_profile_version_is_not_an_integer_identity"),
    ),
    "実行できない制御処理を含む固定版では状態を作らない": (
        (
            RUNTIME_TESTS,
            "test_start_rejects_an_unrunnable_fixed_controller_before_state_creation",
        ),
        (RUNTIME_TESTS, "test_start_rejects_invalid_hook_syntax_before_state_creation"),
        (
            RUNTIME_TESTS,
            "test_start_rejects_invalid_claude_hook_registration_before_state_creation",
        ),
        (
            RUNTIME_TESTS,
            "test_start_rejects_a_noop_claude_hook_registration_before_state_creation",
        ),
        (
            RUNTIME_TESTS,
            "test_start_rejects_a_claude_hook_timeout_above_the_nfr_limit",
        ),
    ),
    "起動途中の停止要求を進捗更新で失わない": (
        (RUNTIME_TESTS, "test_stop_during_core_provision_is_not_overwritten_by_start"),
        (RUNTIME_TESTS, "test_stop_during_pre_manifest_validation_cancels_start"),
        (
            RUNTIME_TESTS,
            "test_timed_out_stop_request_prevents_start_until_stop_is_retried",
        ),
        (
            RUNTIME_TESTS,
            "test_one_completed_stop_does_not_clear_another_live_stop_request",
        ),
        (
            RUNTIME_TESTS,
            "test_successful_stop_clears_a_dangling_stop_request_symlink",
        ),
    ),
    "停止済みの艦隊を再度停止しても外部操作を繰り返さない": (
        (RUNTIME_TESTS, "test_repeated_stop_is_idempotent_without_repeating_external_changes"),
        (RUNTIME_TESTS, "test_stop_retry_reuses_context_invalidation_operation_identity"),
        (CORE_TESTS, "test_context_invalidation_operation_is_idempotent"),
    ),
    "削除中断後に同じ削除を完了させる": (
        (RUNTIME_TESTS, "test_remove_resumes_from_durable_removing_phase"),
        (RUNTIME_TESTS, "test_remove_completes_when_stopped_before_core_database_existed"),
        (RUNTIME_TESTS, "test_remove_delegates_a_partially_initialized_database_to_core"),
        (CORE_TESTS, "test_remove_fleet_is_idempotent"),
        (CORE_TESTS, "test_remove_fleet_treats_a_schema_only_database_as_absent"),
    ),
    "固定実行版の改ざんを実行前に拒否する": (
        (
            RUNTIME_TESTS,
            "test_start_rejects_an_allowlisted_file_reached_through_a_symlink_directory",
        ),
        (
            RUNTIME_TESTS,
            "test_start_rejects_special_permission_bits_on_execution_runtime_directory",
        ),
        (RUNTIME_TESTS, "test_status_rejects_unexpected_file_in_execution_snapshot_before_runner"),
        (RUNTIME_TESTS, "test_status_rejects_execution_snapshot_mode_change_before_runner"),
        (
            RUNTIME_TESTS,
            "test_status_rejects_special_permission_bits_on_an_execution_file",
        ),
        (RUNTIME_TESTS, "test_status_rejects_a_non_string_manifest_phase"),
        (RUNTIME_TESTS, "test_boolean_manifest_format_version_is_rejected"),
        (RUNTIME_TESTS, "test_status_rejects_a_manifest_with_another_launch_identity"),
        (RUNTIME_TESTS, "test_status_rejects_special_permission_bits_on_hook_runtime"),
    ),
    "起動中に導入元の実行物が変わっても固定版だけを使う": (
        (RUNTIME_TESTS, "test_start_uses_snapshot_when_executable_changes_after_capture"),
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
