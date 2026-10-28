import asyncio
import csv
import io
import json
import logging
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tkinter import Tk, Toplevel
from tkinter.font import Font
from PIL import Image
import telegram_caption_downloader_gui as gui

from telegram_caption_downloader_gui import (
    GroupSettingsDialog,
    TelegramDownloaderApp,
    delete_group_profile,
    group_cleanup_targets,
    purge_group_profile,
    dated_group_output,
    build_download_selection,
    build_selection,
    build_note_message_ids,
    capture_status_path,
    clear_old_runtime_logs,
    load_capture_status,
    save_capture_status,
    format_duration,
    format_processing_status,
    thread_stack_dump,
    image_groups_are_similar,
    TIANJI_MIN_COLOR_INTERSECTION,
    TIANJI_MAX_ASPECT_RATIO,
    collect_adjacent_preview_messages,
    collect_adjacent_first_preview_messages,
    ocr_labels_from_payload,
    add_similar_immediate_groups,
    add_first_image_ocr_immediate_groups,
    load_group_settings,
    load_notes,
    load_saved_credentials,
    normalized,
    parse_clock,
    parse_day,
    safe_folder_name,
    source_tag,
    message_refs,
    today_result_exists,
    save_group_profile,
    save_saved_credentials,
    save_session_string,
    split_chat_addresses,
    STATUS_OUTPUT_DIR,
    validate_group_name,
    runtime_log_path,
    notes_for_retry,
    logged_telegram_client,
    account_directory,
    load_account_profiles,
    create_account_profile,
    delete_account_profile,
    migrate_legacy_account,
    save_account_profile,
    verify_account_user,
    load_account_session,
    load_session_string,
    session_blob_path,
    resolve_private_chat,
    DOWNLOAD_CONCURRENCY,
    HUANGDAXIAN_ADJACENT_LABELS,
    HUANGDAXIAN_OCR_LABELS,
    YANRAN_ADJACENT_LABELS,
    YANRAN_FIRST_IMAGE_OCR_LABELS,
    XINAO_EXPERT_UNWANTED_MARKERS,
    compact_text,
    collect_label_group_messages,
    filter_unwanted_ocr_groups,
    OcrError,
    get_ocr_engine,
    load_window_preset,
    save_window_preset,
    WINDOW_PRESETS,
    load_group_notes,
    auto_capture_due,
    normalize_auto_capture_settings,
    load_manual_capture_log,
    save_manual_capture_log,
    manual_capture_log_path,
    record_manual_capture,
)


def fake_session_string(seed: int = 0) -> str:
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(bytes((seed + index) % 256 for index in range(256)))
    return session.save()


def read_report(path: Path) -> list[list[str]]:
    """读取提取报告.csv（兼容 utf-8-sig 表头）；文件不存在时返回空列表。"""
    if not path.is_file():
        return []
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))


class CoreLogicTests(unittest.TestCase):
    def test_auto_capture_settings_validate_and_schedule_boundaries(self):
        config = normalize_auto_capture_settings({
            "enabled": True,
            "first_time": "18:00",
            "second_time": "22:30",
            "min_images": 1000,
        })
        self.assertEqual(config["first_time"], "18:00")
        self.assertEqual(config["second_time"], "22:30")
        self.assertEqual(config["min_images"], 1000)
        day = date(2026, 10, 3)
        before_first = datetime(2026, 10, 3, 17, 59, tzinfo=timezone(timedelta(hours=8)))
        after_first = datetime(2026, 10, 3, 18, 0, tzinfo=before_first.tzinfo)
        after_second = datetime(2026, 10, 3, 22, 30, tzinfo=before_first.tzinfo)
        self.assertIsNone(auto_capture_due(config, before_first))
        self.assertEqual(auto_capture_due(config, after_first), 1)
        config["first_attempt_date"] = day.isoformat()
        self.assertIsNone(auto_capture_due(config, after_first))
        self.assertEqual(auto_capture_due(config, after_second), 2)
        config["captured_date"] = day.isoformat()
        self.assertIsNone(auto_capture_due(config, after_second))
        config["captured_date"] = ""
        config["second_attempt_date"] = day.isoformat()
        self.assertIsNone(auto_capture_due(config, after_second))
        self.assertEqual(auto_capture_due(config, after_first + timedelta(days=1)), 1)
        with self.assertRaises(ValueError):
            normalize_auto_capture_settings({"enabled": True, "first_time": "22:00", "second_time": "21:00", "min_images": 1})
        with self.assertRaises(ValueError):
            normalize_auto_capture_settings({"enabled": True, "first_time": "18:00", "second_time": "22:00", "min_images": 0})

    def test_manual_capture_json_log_retains_seven_days_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = manual_capture_log_path(root)
            today = datetime.now(gui.CN_TZ).date()
            retained_day = today - timedelta(days=6)
            expired_day = today - timedelta(days=7)
            path.write_text(json.dumps({"days": {
                expired_day.isoformat(): ["过期群"],
                retained_day.isoformat(): ["自动群"],
                today.isoformat(): ["自动群"],
            }}, ensure_ascii=False), encoding="utf-8")
            days = load_manual_capture_log(path, today)
            self.assertEqual(days, {retained_day.isoformat(): ["自动群"], today.isoformat(): ["自动群"]})
            record_manual_capture(path, "自动群", today)
            record_manual_capture(path, "手动群", today)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["days"][today.isoformat()],
                ["手动群", "自动群"],
            )
            # A historical or future date must never erase today's completion.
            self.assertTrue(record_manual_capture(path, "历史群", retained_day))
            self.assertFalse(record_manual_capture(path, "过期群", expired_day))
            self.assertFalse(record_manual_capture(path, "未来群", today + timedelta(days=8)))
            days = load_manual_capture_log(path, today)
            self.assertEqual(days[today.isoformat()], ["手动群", "自动群"])
            self.assertEqual(days[retained_day.isoformat()], ["历史群", "自动群"])
            self.assertNotIn(retained_day.isoformat(), load_manual_capture_log(path, today + timedelta(days=1)))

    def test_manual_capture_log_preserves_original_on_invalid_json_or_replace_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = manual_capture_log_path(Path(directory))
            today = datetime.now(gui.CN_TZ).date()
            for content in ('{"broken":', '{"days":{"invalid-date":[]}}'):
                path.write_text(content, encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    record_manual_capture(path, "新群", today)
                self.assertEqual(path.read_bytes(), before)
            path.write_text(json.dumps({"days": {today.isoformat(): ["已抓群"]}}), encoding="utf-8")
            before = path.read_bytes()
            with patch.object(gui.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    record_manual_capture(path, "新群", today)
            self.assertEqual(path.read_bytes(), before)

    def test_group_settings_keep_normalized_auto_capture_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "群配置").mkdir()
            (root / "群配置" / "groups.json").write_text(json.dumps({
                "groups": [{
                    "name": "自动群", "address": "@auto", "start_time": "00:00", "end_time": "23:59",
                    "auto_capture": {"enabled": True, "first_time": "18:00", "second_time": "22:30", "min_images": 1000},
                }],
                "selected_group": "自动群",
                "output_path": str(root / "result"),
            }, ensure_ascii=False), encoding="utf-8")
            settings = load_group_settings(root)
            self.assertEqual(settings["groups"][0]["auto_capture"]["first_time"], "18:00")
            self.assertEqual(settings["groups"][0]["auto_capture"]["min_images"], 1000)

    def test_invalid_auto_capture_configuration_does_not_discard_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "群配置").mkdir()
            config = {"enabled": True, "first_time": "bad", "second_time": "22:00", "min_images": 1000}
            path = root / "群配置" / "groups.json"
            payload = {"groups": [{"name": "保留群", "address": "@keep", "auto_capture": config}]}
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            original = path.read_bytes()
            loaded = load_group_settings(root, read_only=True)
            self.assertEqual(loaded["groups"][0]["name"], "保留群")
            self.assertEqual(loaded["groups"][0]["auto_capture"], config)
            self.assertEqual(path.read_bytes(), original)

    def test_today_result_folder_controls_first_capture_state(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            target_day = date(2026, 9, 5)
            self.assertFalse(today_result_exists(output, target_day, "新澳六合彩资料"))
            dated_group_output(output, target_day, "新澳六合彩资料").mkdir(parents=True)
            self.assertTrue(today_result_exists(output, target_day, "新澳六合彩资料"))

    def test_yanran_new_notes_are_configured_for_two_sided_adjacent_matching(self):
        self.assertEqual(
            YANRAN_ADJACENT_LABELS,
            {"乖乖团队", "天机阁特围", "天机阁杀料", "恩平"},
        )
        notes = {
            "天机阁特围": "天机阁特围",
            "天机阁杀料": "天机阁杀料",
        }
        messages = [
            SimpleNamespace(id=1, raw_text="", grouped_id=10, visual_type="blue"),
            SimpleNamespace(id=2, raw_text="天机阁杀料", grouped_id=20, visual_type="blue"),
            SimpleNamespace(id=3, raw_text="", grouped_id=20, visual_type="blue"),
            SimpleNamespace(id=4, raw_text="", grouped_id=30, visual_type="blue"),
            SimpleNamespace(id=5, raw_text="", grouped_id=40, visual_type="red"),
        ]
        selected = build_download_selection(
            messages, notes, special_adjacent_labels=set(notes.values()),
            bidirectional_adjacent_labels=set(notes.values()),
            group_similarity=lambda anchor, candidate: anchor[0].visual_type == candidate[0].visual_type,
        )
        self.assertEqual(selected[1], {"天机阁杀料"})
        self.assertEqual(selected[4], {"天机阁杀料"})
        self.assertNotIn(5, selected)

    def test_yanran_tianji_labels_use_first_image_ocr_for_neighbors(self):
        self.assertEqual(
            YANRAN_FIRST_IMAGE_OCR_LABELS,
            {"天机阁特围", "天机阁杀料"},
        )

    def test_enping_extends_two_groups_in_both_directions(self):
        messages = [
            SimpleNamespace(id=index, raw_text="恩平" if index == 3 else "", grouped_id=index)
            for index in range(1, 7)
        ]
        labels = YANRAN_ADJACENT_LABELS.intersection({"恩平"})
        checked = []
        selected = build_download_selection(
            messages, {"恩平": "恩平"}, special_adjacent_labels=labels,
            bidirectional_adjacent_labels=labels,
            similarity_labels=labels - YANRAN_FIRST_IMAGE_OCR_LABELS,
            group_similarity=lambda anchor, candidate: checked.append(candidate[0].id) or True,
        )
        self.assertEqual(set(checked), {1, 2, 4, 5})
        self.assertEqual(selected, {index: {"恩平"} for index in range(1, 6)})

    def test_tianji_adjacent_labels_can_skip_visual_similarity(self):
        messages = [
            SimpleNamespace(id=1, raw_text="", grouped_id=10, visual_type="blue"),
            SimpleNamespace(id=2, raw_text="天机阁杀料", grouped_id=20, visual_type="blue"),
            SimpleNamespace(id=3, raw_text="", grouped_id=20, visual_type="blue"),
            SimpleNamespace(id=4, raw_text="", grouped_id=30, visual_type="blue"),
        ]
        similarity_calls = []
        selected = build_download_selection(
            messages,
            {"天机阁杀料": "天机阁杀料"},
            special_adjacent_labels={"天机阁杀料"},
            bidirectional_adjacent_labels={"天机阁杀料"},
            similarity_labels=set(),
            group_similarity=lambda *_: similarity_calls.append(True) or True,
        )
        self.assertEqual(similarity_calls, [])
        self.assertEqual(set(selected), {2, 3})

    def test_first_image_preview_collection_only_returns_neighbor_first_images(self):
        messages = [
            SimpleNamespace(id=2, raw_text="", grouped_id=10),
            SimpleNamespace(id=1, raw_text="", grouped_id=10),
            SimpleNamespace(id=4, raw_text="天机阁杀料", grouped_id=20),
            SimpleNamespace(id=3, raw_text="", grouped_id=20),
            SimpleNamespace(id=6, raw_text="", grouped_id=30),
            SimpleNamespace(id=5, raw_text="", grouped_id=30),
        ]
        self.assertEqual(
            collect_adjacent_first_preview_messages(
                messages,
                {3: {"天机阁杀料"}, 4: {"天机阁杀料"}},
                {"天机阁杀料"},
                {"天机阁杀料"},
            ),
            {1, 5},
        )

    def test_first_image_ocr_classifies_whole_neighbor_and_never_reads_second_image(self):
        messages = [
            SimpleNamespace(id=2, raw_text="", grouped_id=10),
            SimpleNamespace(id=1, raw_text="", grouped_id=10),
            SimpleNamespace(id=4, raw_text="天机阁杀料", grouped_id=20),
            SimpleNamespace(id=3, raw_text="", grouped_id=20),
            SimpleNamespace(id=6, raw_text="", grouped_id=30),
            SimpleNamespace(id=5, raw_text="", grouped_id=30),
        ]
        calls = []

        def fake_ocr(payload, labels, _engine, on_error=None):
            calls.append(payload)
            return {"天机阁杀料"} if payload == b"hit-first" else set()

        with patch("telegram_caption_downloader_gui.ocr_labels_from_payload", side_effect=fake_ocr):
            selected = add_first_image_ocr_immediate_groups(
                messages,
                {3: {"天机阁杀料"}, 4: {"天机阁杀料"}},
                {"天机阁杀料"},
                {1: b"miss-first", 2: b"hit-second", 5: b"hit-first", 6: b"ignored-second"},
                ocr_engine=object(),
                bidirectional_labels={"天机阁杀料"},
            )
        self.assertEqual(set(calls), {b"miss-first", b"hit-first"})
        self.assertNotIn(b"hit-second", calls)
        self.assertNotIn(b"ignored-second", calls)
        self.assertEqual(selected[5], {"天机阁杀料"})
        self.assertEqual(selected[6], {"天机阁杀料"})
        self.assertNotIn(1, selected)
        self.assertNotIn(2, selected)

    def test_tianji_rechecks_new_neighbor_pair_without_repeating_ocr(self):
        label = {"天机阁杀料"}
        messages = [SimpleNamespace(id=i, raw_text="", grouped_id=i) for i in range(1, 5)]
        selection = {1: set(label), 4: set(label)}
        checked, cache = set(), {}
        pairs = []

        def compare(anchor, candidate):
            pairs.append((anchor[0].id, candidate[0].id))
            return anchor[0].id == 3

        with patch("telegram_caption_downloader_gui.ocr_labels_from_payload",
                   side_effect=lambda payload, *a, **kw: label if payload == b"3" else set()) as ocr:
            for expected in ({2, 3}, {2}):
                self.assertEqual(collect_adjacent_first_preview_messages(
                    messages, selection, label, label, checked_pairs=checked), expected)
                selection = add_first_image_ocr_immediate_groups(
                    messages, selection, label, {2: b"2", 3: b"3"}, bidirectional_labels=label,
                    checked_pairs=checked, ocr_cache=cache, group_similarity=compare,
                )
            self.assertEqual(ocr.call_count, 2)
        self.assertEqual(pairs, [(1, 2), (3, 2)])
        self.assertEqual(set(selection), {1, 2, 3, 4})

    def test_huangdaxian_first_image_classifies_by_detected_label_and_skips_second_image(self):
        labels = {"战狼", "68", "红人馆", "香奈儿"}
        self.assertEqual(HUANGDAXIAN_ADJACENT_LABELS, labels)
        self.assertEqual(HUANGDAXIAN_OCR_LABELS, labels)
        messages = [
            SimpleNamespace(id=1, raw_text="", grouped_id=10),
            SimpleNamespace(id=2, raw_text="", grouped_id=10),
            SimpleNamespace(id=3, raw_text="战狼", grouped_id=20),
            SimpleNamespace(id=4, raw_text="", grouped_id=20),
            SimpleNamespace(id=5, raw_text="", grouped_id=30),
            SimpleNamespace(id=6, raw_text="", grouped_id=30),
        ]
        calls = []

        def fake_ocr(payload, requested_labels, _engine, on_error=None):
            calls.append((payload, requested_labels))
            return {"68"} if payload == b"hit-68" else set()

        with patch("telegram_caption_downloader_gui.ocr_labels_from_payload", side_effect=fake_ocr):
            selected = add_first_image_ocr_immediate_groups(
                messages,
                {3: {"战狼"}, 4: {"战狼"}},
                labels,
                {1: b"hit-68", 2: b"second-ignored", 5: b"miss", 6: b"second-ignored"},
                ocr_engine=object(),
                bidirectional_labels=labels,
                detected_labels=labels,
            )
        self.assertEqual(calls, [(b"hit-68", labels), (b"miss", labels)])
        self.assertEqual(selected[1], {"68"})
        self.assertEqual(selected[2], {"68"})
        self.assertNotIn(5, selected)
        self.assertNotIn(6, selected)

    def test_ocr_recognizes_tianji_marker_and_rejects_unrelated_chart(self):
        class FakeOCR:
            def __init__(self, text):
                self.text = text

            def predict(self, image):
                return [{"rec_texts": [self.text]}]

        payload = io.BytesIO()
        Image.new("RGB", (4, 4), (255, 255, 255)).save(payload, format="PNG")
        self.assertEqual(
            ocr_labels_from_payload(
                payload.getvalue(), {"天机阁特围"},
                FakeOCR("天机阁论坛齐天大圣新澳"),
            ),
            {"天机阁特围"},
        )
        self.assertEqual(
            ocr_labels_from_payload(
                payload.getvalue(), {"天机阁杀料"},
                FakeOCR("天机阁论坛天之涯新澳"),
            ),
            {"天机阁杀料"},
        )
        self.assertEqual(
            ocr_labels_from_payload(
                payload.getvalue(), {"天机阁特围", "天机阁杀料"},
                FakeOCR("新澳走势 期数 特码"),
            ),
            set(),
        )
        for text in ("天机阁", "天機閣", "天机", "天王", "手机", "天機"):
            with self.subTest(text=text):
                labels = {"天机阁特围", "天机阁杀料"}
                self.assertEqual(
                    ocr_labels_from_payload(payload.getvalue(), labels, FakeOCR(text)),
                    labels if text in {"天机阁", "天機閣", "天机"} else set(),
                )

    def test_local_account_selection_validates_id_and_preserves_secrets(self):
        from telegram_caption_downloader_gui import load_selected_account, save_selected_account
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            account_id = create_account_profile(root, "选择号", 123, "test-secret", "+8613800000000")["id"]
            credentials = account_directory(root, account_id) / "credentials.bin"
            before = credentials.read_bytes()
            self.assertEqual(load_selected_account(root), "")
            save_selected_account(root, account_id)
            self.assertEqual(load_selected_account(root), account_id)
            preference = root / "account_selection.json"
            self.assertEqual(json.loads(preference.read_text(encoding="utf-8")), {"account_id": account_id})
            with self.assertRaises(ValueError):
                save_selected_account(root, "../other")
            with patch("telegram_caption_downloader_gui.os.replace", side_effect=OSError("test write failure")):
                with self.assertRaises(OSError):
                    save_selected_account(root, account_id)
            self.assertEqual(load_selected_account(root), account_id)
            self.assertEqual(credentials.read_bytes(), before)
            preference.write_text('{"account_id":"missing"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_selected_account(root)

    def test_status_and_download_use_same_exact_caption_rule(self):
        messages = [SimpleNamespace(id=1, raw_text="🌟战狼", grouped_id=None),
                    SimpleNamespace(id=2, raw_text="🌟战狼、赌神", grouped_id=None),
                    SimpleNamespace(id=3, raw_text="陌生人大小姐", grouped_id=None)]
        notes = {"战狼": "战狼", "大小姐": "大小姐"}
        self.assertEqual(build_selection(messages, notes, exact_labels={"战狼"}),
                         {1: {"战狼"}, 3: {"大小姐"}})
        self.assertEqual(build_note_message_ids(messages, notes, exact_labels={"战狼"}),
                         {"战狼": {1}, "大小姐": {3}})

    def test_legacy_root_account_is_migrated_into_a_normal_account(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_saved_credentials(root / "credentials.bin", 123, "original-hash", "+8613800000000")
            save_session_string(root / "session.bin", "legacy-session")
            (root / "account.session").write_bytes(b"original session")
            (root / "accounts.json").write_text(
                json.dumps({"accounts": [{"id": "legacy", "name": "魂", "user_id": 42}]}), encoding="utf-8")
            (root / "account_selection.json").write_text('{"account_id":"legacy"}', encoding="utf-8")
            account_id = migrate_legacy_account(root)
            self.assertRegex(account_id, r"^[0-9a-f]{32}$")
            self.assertEqual(load_account_profiles(root), [{"id": account_id, "name": "魂", "user_id": 42}])
            folder = account_directory(root, account_id)
            self.assertEqual(load_saved_credentials(folder / "credentials.bin"), (123, "original-hash", "+8613800000000"))
            self.assertEqual(load_session_string(folder / "session.bin"), "legacy-session")
            self.assertEqual((folder / "account.session").read_bytes(), b"original session")
            self.assertEqual(json.loads((root / "account_selection.json").read_text(encoding="utf-8")),
                             {"account_id": account_id})
            for leftover in ("credentials.bin", "session.bin", "account.session"):
                self.assertFalse((root / leftover).exists())
            # 幂等：根目录已无凭据，再跑一次不应产生第二个账号。
            self.assertEqual(migrate_legacy_account(root), "")
            self.assertEqual(len(load_account_profiles(root)), 1)

    def test_legacy_migration_failure_keeps_root_files_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_saved_credentials(root / "credentials.bin", 123, "original-hash", "+8613800000000")
            before = (root / "credentials.bin").read_bytes()
            with patch("telegram_caption_downloader_gui.shutil.copy2", side_effect=OSError("test copy failure")):
                with self.assertRaises(OSError):
                    migrate_legacy_account(root)
            self.assertEqual((root / "credentials.bin").read_bytes(), before)
            self.assertFalse((root / "accounts.json").exists())
            self.assertEqual(list((root / "accounts").glob("*")), [])

    def test_legacy_migration_is_skipped_once_the_manifest_has_no_legacy_entry(self):
        # 清单已切走但根目录原件残留（例如上次删原件失败）时，绝不能凭空多建一个账号。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_saved_credentials(root / "credentials.bin", 123, "original-hash", "+8613800000000")
            existing = create_account_profile(root, "已有号", 456, "other-hash", "+8613900000000")
            self.assertEqual(migrate_legacy_account(root), "")
            self.assertEqual([item["id"] for item in load_account_profiles(root)], [existing["id"]])
            self.assertEqual(list((root / "accounts").glob("*")), [account_directory(root, existing["id"])])

    def test_legacy_sqlite_session_is_migrated_to_encrypted_string(self):
        from telethon.crypto import AuthKey
        from telethon.sessions import SQLiteSession, StringSession
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = account_directory(root, "1" * 32)
            folder.mkdir(parents=True)
            session = folder / "account"
            old = SQLiteSession(str(session))
            old.set_dc(2, "149.154.167.51", 443)
            old.auth_key = AuthKey(bytes(range(256)))
            expected = StringSession.save(old)
            old.close()
            migrated = load_account_session(session, lambda _text: None)
            self.assertEqual(migrated.save(), expected)
            self.assertEqual(load_session_string(session_blob_path(folder)), expected)
            self.assertTrue((folder / "account.session").is_file())

    def test_private_group_settings_roundtrip_and_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = load_group_settings(root)
            save_group_profile(root, settings, "", "私密群", "",
                               chat_id=-1004401898428, bound_account_id="1" * 32)
            reloaded = load_group_settings(root)
            self.assertEqual(reloaded["groups"], [{
                "name": "私密群",
                "address": "",
                "start_time": "00:00",
                "end_time": "23:59",
                "chat_id": -1004401898428,
                "bound_account_id": "1" * 32,
            }])
            save_group_profile(root, reloaded, "私密群", "公开群", "https://t.me/example")
            again = load_group_settings(root)
            self.assertEqual(again["groups"], [{
                "name": "公开群",
                "address": "https://t.me/example",
                "start_time": "00:00",
                "end_time": "23:59",
            }])
            with self.assertRaises(ValueError):
                save_group_profile(root, again, "", "私密二", "", chat_id=-1001)
            with self.assertRaises(ValueError):
                save_group_profile(root, again, "", "私密三", "", chat_id=1001, bound_account_id="1" * 32)

    def test_group_notes_support_per_link_exclusive_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "群.json"
            path.write_text(json.dumps({
                "keywords": ["战狼", "香奈儿", "绿图/⭐绿图"],
                "links": {"https://t.me/amlhmfzl": ["综合", "两版绿杀+完美杀"]},
            }, ensure_ascii=False), encoding="utf-8")
            default_notes, address_notes = load_group_notes(path)
            self.assertEqual(default_notes["战狼"], "战狼")
            self.assertEqual(default_notes["⭐绿图"], "绿图")
            self.assertEqual(
                address_notes,
                {"https://t.me/amlhmfzl": {"综合": "综合", "两版绿杀+完美杀": "两版绿杀+完美杀"}},
            )
            path.write_text(json.dumps({"keywords": [], "links": {"x": []}}, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_group_notes(path)
            path.write_text(json.dumps({"keywords": [], "links": ["x"]}, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_group_notes(path)
            path.write_text(json.dumps({"keywords": ["战狼"]}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(load_group_notes(path), ({"战狼": "战狼"}, {}))

    def test_window_preset_round_trip_and_validation(self):
        self.assertEqual(WINDOW_PRESETS, {"2k": (1600, 960), "1080": (1440, 860)})
        self.assertNotEqual(WINDOW_PRESETS["2k"], WINDOW_PRESETS["1080"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(load_window_preset(root), "")
            save_window_preset(root, "2k")
            self.assertEqual(load_window_preset(root), "2k")
            self.assertEqual(
                json.loads((root / "ui_settings.json").read_text(encoding="utf-8")),
                {"window_preset": "2k"},
            )
            save_window_preset(root, "1080")
            self.assertEqual(load_window_preset(root), "1080")
            (root / "ui_settings.json").write_text('{"window_preset":"4k"}', encoding="utf-8")
            self.assertEqual(load_window_preset(root), "")
            with self.assertRaises(ValueError):
                save_window_preset(root, "8k")

    def test_resolve_private_chat_uses_cache_then_dialogs(self):
        from telethon import utils
        from telethon.tl.types import InputPeerChannel
        chat_id = -1004401898428
        peer = InputPeerChannel(channel_id=4401898428, access_hash=7)
        self.assertEqual(utils.get_peer_id(peer), chat_id)

        class CachedClient:
            def __init__(self):
                self.requested = None
                self.dialogs_scanned = False

            def get_input_entity(self, value):
                self.requested = value
                return peer

            def iter_dialogs(self):
                self.dialogs_scanned = True
                return iter(())

        client = CachedClient()
        self.assertIs(resolve_private_chat(client, chat_id), peer)
        self.assertEqual(client.requested, chat_id)
        self.assertFalse(client.dialogs_scanned)

        class Dialog:
            id = chat_id
            input_entity = peer

        class FallbackClient:
            def get_input_entity(self, value):
                raise ValueError("not cached")

            def iter_dialogs(self):
                return iter([Dialog()])

        self.assertIs(resolve_private_chat(FallbackClient(), chat_id), peer)

        class MissingClient(FallbackClient):
            def iter_dialogs(self):
                return iter(())

        with self.assertRaisesRegex(ValueError, "无法定位"):
            resolve_private_chat(MissingClient(), chat_id)

    def test_muxi_exact_labels_require_full_match(self):
        from telegram_caption_downloader_gui import MUXI_EXACT_LABELS, caption_matches_keyword
        self.assertEqual(MUXI_EXACT_LABELS, {"九肖", "绝杀合数", "帅铁精杀", "四头中特24码中特", "大围"})
        self.assertTrue(caption_matches_keyword("九肖", "九肖", True))
        self.assertTrue(caption_matches_keyword("  九 肖  ", "九肖", True))
        self.assertFalse(caption_matches_keyword("九肖统计", "九肖", True))
        self.assertFalse(caption_matches_keyword("单挑一肖", "九肖", True))
        self.assertTrue(caption_matches_keyword("九肖统计", "九肖统计", False))
        self.assertTrue(caption_matches_keyword("四头中特 24码中特", "四头中特24码中特", True))
        self.assertFalse(caption_matches_keyword("四头中特24码中特加", "四头中特24码中特", True))

    def test_slash_aliases_are_independent_keywords_not_space_split(self):
        from telegram_caption_downloader_gui import _notes_from_entries
        notes = _notes_from_entries(["阿尔法天狼星/阿尔法/天狼星"])
        self.assertEqual(
            notes,
            {"阿尔法天狼星": "阿尔法天狼星", "阿尔法": "阿尔法天狼星", "天狼星": "阿尔法天狼星"},
        )
        spaced = _notes_from_entries(["阿尔法天狼星/阿尔法 天狼星"])
        self.assertEqual(spaced, {"阿尔法天狼星": "阿尔法天狼星", "阿尔法 天狼星": "阿尔法天狼星"})

    def test_alpha_or_sirius_caption_falls_into_alpha_sirius_folder(self):
        from telegram_caption_downloader_gui import _notes_from_entries
        notes = _notes_from_entries(["阿尔法天狼星/阿尔法/天狼星"])
        messages = [
            SimpleNamespace(id=1, raw_text="阿尔法", grouped_id=None),
            SimpleNamespace(id=2, raw_text="天狼星", grouped_id=None),
            SimpleNamespace(id=3, raw_text="阿尔法天狼星", grouped_id=None),
            SimpleNamespace(id=4, raw_text="阿尔法 天狼星", grouped_id=None),
            SimpleNamespace(id=5, raw_text="无关备注", grouped_id=None),
        ]
        selection = build_selection(messages, notes)
        for message_id in (1, 2, 3, 4):
            self.assertEqual(selection[message_id], {"阿尔法天狼星"}, f"消息 {message_id} 未归入阿尔法天狼星")
        self.assertFalse(selection.get(5))

    def test_accounts_use_separate_sessions_and_encrypted_credentials_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = create_account_profile(root, "第一个号", 123, "old-hash", "+8613800000000")
            new = create_account_profile(root, "第二个号", 456, "new-secret", "+8613900000000")
            self.assertNotEqual(account_directory(root, new["id"]), account_directory(root, first["id"]))
            self.assertEqual(load_saved_credentials(account_directory(root, new["id"]) / "credentials.bin"),
                             (456, "new-secret", "+8613900000000"))
            self.assertEqual(load_saved_credentials(account_directory(root, first["id"]) / "credentials.bin"),
                             (123, "old-hash", "+8613800000000"))
            manifest = (root / "accounts.json").read_text(encoding="utf-8")
            self.assertNotIn("new-secret", manifest)
            self.assertNotIn("13900000000", manifest)
            with self.assertRaises(ValueError):
                create_account_profile(root, "重复", 456, "new-secret", "+86 139-0000-0000")
            self.assertEqual(len(load_account_profiles(root)), 2)

    def test_delete_account_profile_removes_manifest_entry_directory_and_default_selection(self):
        from telegram_caption_downloader_gui import save_selected_account
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keep = create_account_profile(root, "保留号", 123, "old-secret", "+8613800000000")
            new = create_account_profile(root, "待删除号", 456, "new-secret", "+8613900000000")
            folder = account_directory(root, new["id"])
            save_session_string(session_blob_path(folder), "session-string")
            save_selected_account(root, new["id"])
            delete_account_profile(root, new["id"])
            self.assertFalse(folder.exists())
            self.assertEqual([item["id"] for item in load_account_profiles(root)], [keep["id"]])
            self.assertEqual(load_saved_credentials(account_directory(root, keep["id"]) / "credentials.bin"),
                             (123, "old-secret", "+8613800000000"))
            self.assertFalse((root / "account_selection.json").exists())

    def test_delete_account_profile_rejects_missing_and_keeps_files_on_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                delete_account_profile(root, "1" * 32)
            new = create_account_profile(root, "待删除号", 456, "new-secret", "+8613900000000")
            folder = account_directory(root, new["id"])
            with patch("telegram_caption_downloader_gui.os.replace", side_effect=OSError("test write failure")):
                with self.assertRaises(OSError):
                    delete_account_profile(root, new["id"])
            self.assertTrue(folder.exists())
            self.assertEqual(load_saved_credentials(folder / "credentials.bin"), (456, "new-secret", "+8613900000000"))
            self.assertEqual(len(load_account_profiles(root)), 1)

    def test_account_profile_rejects_path_traversal_and_corrupt_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for invalid in ("../account", "C:/temp", "", "..", "../../other"):
                with self.assertRaises(ValueError):
                    account_directory(root, invalid)
            (root / "accounts.json").write_text('{"accounts": [{"id": "../other", "name": "bad"}]}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_account_profiles(root)

    def test_login_verifies_both_telegram_id_and_phone(self):
        profile = {"id": "1" * 32, "name": "旧号", "user_id": 7}
        verify_account_user(profile, SimpleNamespace(id=7, phone="8613800000000"), "+8613800000000")
        with self.assertRaises(RuntimeError):
            verify_account_user(profile, SimpleNamespace(id=8, phone="8613800000000"), "+8613800000000")
        with self.assertRaises(RuntimeError):
            verify_account_user(profile, SimpleNamespace(id=7, phone="8613900000000"), "+8613800000000")

    def test_actual_telethon_flood_wait_is_logged_without_network_or_real_wait(self):
        from unittest.mock import AsyncMock
        from telethon.errors import FloodWaitError
        from telethon.sessions import MemorySession
        from telethon.tl.functions import PingRequest

        events = []
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            with logged_telegram_client(MemorySession(), 123, "test", events.append) as client:
                request = PingRequest(1)
                sender = SimpleNamespace(send=AsyncMock(side_effect=[FloodWaitError(request, capture=20), True]))
                with patch("telethon.client.users.asyncio.sleep", new_callable=AsyncMock) as sleep:
                    self.assertTrue(loop.run_until_complete(client._call(sender, request)))
                    sleep.assert_awaited_once_with(20)
                self.assertEqual(sender.send.call_count, 2)
            self.assertTrue(any("【限流】FLOOD_WAIT" in event and "20 秒" in event for event in events))
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    def test_ocr_error_is_reported_without_dumping_image_or_exception_contents(self):
        events = []
        with self.assertRaises(OcrError):
            ocr_labels_from_payload(b"bad image", {"战狼"}, object(), on_error=events.append)
        self.assertEqual(events, ["UnidentifiedImageError"])

    def test_gpu_ocr_engine_uses_v6_small_and_gpu0_fp32(self):
        import types
        import telegram_caption_downloader_gui as module

        calls = []

        class FakePaddleOCR:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        fake_paddle = types.SimpleNamespace(
            __version__="3.2.2",
            is_compiled_with_cuda=lambda: True,
            set_device=lambda value: calls.append({"set_device": value}),
            device=types.SimpleNamespace(
                cuda=types.SimpleNamespace(device_count=lambda: 1),
                get_device=lambda: "gpu:0",
            ),
        )
        fake_paddleocr = types.SimpleNamespace(PaddleOCR=FakePaddleOCR)
        previous_engine, previous_error = module._OCR_ENGINE, module._OCR_ENGINE_ERROR
        module._OCR_ENGINE = None
        module._OCR_ENGINE_ERROR = None
        try:
            with patch.dict(sys.modules, {"paddle": fake_paddle, "paddleocr": fake_paddleocr}):
                engine = get_ocr_engine()
            self.assertIsInstance(engine, FakePaddleOCR)
            self.assertEqual(calls[0], {"set_device": "gpu:0"})
            self.assertEqual(calls[1]["text_detection_model_name"], "PP-OCRv6_small_det")
            self.assertEqual(calls[1]["text_recognition_model_name"], "PP-OCRv6_small_rec")
            self.assertEqual(calls[1]["device"], "gpu:0")
            self.assertEqual(calls[1]["precision"], "fp32")
            self.assertFalse(calls[1]["enable_hpi"])
            self.assertFalse(calls[1]["use_tensorrt"])
            self.assertFalse(calls[1]["enable_mkldnn"])
        finally:
            module._OCR_ENGINE, module._OCR_ENGINE_ERROR = previous_engine, previous_error

    def test_unwanted_marker_matching_ignores_punctuation_and_spaces(self):
        class FakeOCR:
            def predict(self, image):
                return [{"rec_texts": ["255期： 「小数」 + 「双数」 开：", "实力 双波"]}]

        payload = io.BytesIO()
        Image.new("RGB", (4, 4), (255, 255, 255)).save(payload, format="PNG")
        self.assertEqual(
            ocr_labels_from_payload(
                payload.getvalue(),
                {"小数+双数", "实力双波", "天地中特"},
                FakeOCR(),
                cleanup=compact_text,
            ),
            {"小数+双数", "实力双波"},
        )

    def test_filter_unwanted_ocr_groups_drops_marker_group_and_keeps_other(self):
        messages = [
            SimpleNamespace(id=1, raw_text="斩杀系列", grouped_id=10),
            SimpleNamespace(id=2, raw_text="", grouped_id=10),
            SimpleNamespace(id=3, raw_text="斩杀系列", grouped_id=20),
            SimpleNamespace(id=4, raw_text="", grouped_id=20),
        ]
        selection = {message.id: {"斩杀系列"} for message in messages}
        payloads = {1: b"unwanted", 2: b"unwanted-second", 3: b"wanted", 4: b"wanted-second"}
        calls = []

        def fake_ocr(payload, labels, _engine, on_error=None, cleanup=None):
            calls.append(payload)
            return {"实力双波"} if payload == b"unwanted" else set()

        with patch("telegram_caption_downloader_gui.ocr_labels_from_payload", side_effect=fake_ocr):
            filtered, ignored = filter_unwanted_ocr_groups(
                messages,
                selection,
                {"斩杀系列"},
                payloads,
                XINAO_EXPERT_UNWANTED_MARKERS,
                ocr_engine=object(),
            )
        self.assertEqual(filtered, {3: {"斩杀系列"}, 4: {"斩杀系列"}})
        self.assertEqual(ignored, {"斩杀系列": {1, 2}})
        self.assertEqual(calls, [b"unwanted", b"wanted", b"wanted-second"])

    def test_filter_unwanted_ocr_groups_keeps_all_without_marker_hits(self):
        messages = [SimpleNamespace(id=1, raw_text="斩杀系列", grouped_id=10)]
        with patch("telegram_caption_downloader_gui.ocr_labels_from_payload", return_value=set()):
            filtered, ignored = filter_unwanted_ocr_groups(
                messages,
                {1: {"斩杀系列"}},
                {"斩杀系列"},
                {1: b"payload"},
                XINAO_EXPERT_UNWANTED_MARKERS,
                ocr_engine=object(),
            )
        self.assertEqual(filtered, {1: {"斩杀系列"}})
        self.assertEqual(ignored, {})

    def test_collect_label_group_messages_returns_whole_matching_groups(self):
        messages = [
            SimpleNamespace(id=1, grouped_id=10),
            SimpleNamespace(id=2, grouped_id=10),
            SimpleNamespace(id=3, grouped_id=20),
            SimpleNamespace(id=4, grouped_id=30),
        ]
        self.assertEqual(
            collect_label_group_messages(messages, {1: {"斩杀系列"}}, {"斩杀系列"}),
            {1, 2},
        )
        self.assertEqual(
            collect_label_group_messages(messages, {3: {"其他"}}, {"斩杀系列"}),
            set(),
        )

    def test_telegram_internal_logs_are_filtered_isolated_and_detached(self):
        events = []
        clients = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.logger = kwargs["base_logger"].getChild("client.users")
                self.disconnected = False
                self.kwargs = kwargs
                clients.append(self)

            def disconnect(self):
                self.disconnected = True

        with patch("telethon.sync.TelegramClient", FakeClient):
            for _ in range(2):
                with logged_telegram_client("unused", 123, "SECRET", events.append) as client:
                    client.logger.info("Sleeping%s for %ds (%s) on %s flood wait", "", 20, "0:00:20", "GetFileRequest")
                    client.logger.info("Got timeout while downloading file, retrying once")
                    client.logger.info("Closing current connection to begin reconnect...")
                    client.logger.debug("SECRET")
                    client.logger.info("Received response without parent request: %s", "SECRET")
                    client.logger.warning("Request dump: %s", "SECRET")
                count = len(events)
                client.logger.info("Got timeout while downloading file, retrying once")
                self.assertEqual(len(events), count)
        self.assertTrue(all(client.disconnected for client in clients))
        self.assertEqual(sum("FLOOD_WAIT" in event for event in events), 2)
        self.assertTrue(any("20 秒" in event and "GetFileRequest" in event for event in events))
        self.assertTrue(any("【网络】" in event and "超时" in event for event in events))
        self.assertTrue(any("重连" in event for event in events))
        self.assertNotIn("SECRET", "\n".join(events))

    def test_telegram_log_scope_cleans_up_after_failure(self):
        from unittest.mock import MagicMock
        client = MagicMock()
        with patch("telethon.sync.TelegramClient", return_value=client) as factory:
            with self.assertRaisesRegex(RuntimeError, "test"):
                with logged_telegram_client("unused", 123, "SECRET", lambda text: None):
                    raise RuntimeError("test")
        client.disconnect.assert_called_once()
        self.assertTrue(all(isinstance(handler, logging.NullHandler)
                            for handler in factory.call_args.kwargs["base_logger"].handlers))

    def test_selection_logs_caption_and_adjacent_group_reason(self):
        messages = [SimpleNamespace(id=1, grouped_id=10, raw_text="战狼", chat_id=99),
                    SimpleNamespace(id=2, grouped_id=20, raw_text="", chat_id=99)]
        events = []
        selected = build_download_selection(
            messages, {"战狼": "战狼"}, special_adjacent_labels={"战狼"},
            bidirectional_adjacent_labels={"战狼"}, exact_labels={"战狼"},
            group_similarity=lambda a, b: True, log=events.append,
        )
        self.assertEqual(selected, {1: {"战狼"}, 2: {"战狼"}})
        self.assertTrue(any("关键词" in text and "战狼" in text for text in events))
        self.assertTrue(any("相似" in text and "20" in text for text in events))

    def test_runtime_logs_midnight_clears_all_categories_and_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for group in ("嫣然心水", "黄大仙新澳", "系统"):
                path = clear_old_runtime_logs(root, date(2026, 9, 2), group)
                path.write_text("【限流】FLOOD_WAIT\n【网络】重连\n【汇总】完成", encoding="utf-8")
            clear_old_runtime_logs(root, date(2026, 9, 3), "嫣然心水")
            self.assertEqual(list(root.rglob("2026-09-02.log")), [])
            self.assertEqual(runtime_log_path(root, date(2026, 9, 3), "嫣然心水").read_text(), "")
            current = root / "黄大仙新澳" / "2026-09-03.log"
            current.write_text("today", encoding="utf-8")
            other = root / "黄大仙新澳" / "抓取状态.json"
            other.write_text("{}", encoding="utf-8")
            clear_old_runtime_logs(root, date(2026, 9, 3), "嫣然心水")
            self.assertEqual(current.read_text(), "today")
            self.assertEqual(other.read_text(), "{}")

    def test_downloader_status_directory_is_separate_from_ocr_configuration(self):
        self.assertEqual(
            STATUS_OUTPUT_DIR,
            Path(r"C:\Users\Administrator\Desktop\每天工具\飞机抓图\outputs\抓取状态"),
        )
        self.assertNotEqual(
            STATUS_OUTPUT_DIR,
            Path(r"C:\Users\Administrator\Desktop\每天工具\飞机抓到的分类\outputs\配置文件"),
        )

    def test_split_chat_addresses_supports_two_links_and_deduplicates(self):
        self.assertEqual(
            split_chat_addresses(" https://t.me/xam49 | https://t.me/xamlh\nhttps://t.me/xam49 "),
            ["https://t.me/xam49", "https://t.me/xamlh"],
        )

    def test_group_profile_round_trips_multiple_chat_addresses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = load_group_settings(root)
            save_group_profile(
                root,
                settings,
                "",
                "双群配置",
                "https://t.me/xam49 | https://t.me/xamlh",
            )
            loaded = load_group_settings(root)
            self.assertEqual(
                loaded["groups"][0]["address"],
                "https://t.me/xam49 | https://t.me/xamlh",
            )
    def test_runtime_logs_keep_only_current_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "嫣然心水" / "2026-08-31.log"
            old.parent.mkdir()
            old.write_text("old", encoding="utf-8")
            active = clear_old_runtime_logs(root, date(2026, 9, 1), "嫣然心水")
            self.assertEqual(active, runtime_log_path(root, date(2026, 9, 1), "嫣然心水"))
            self.assertTrue(active.exists())
            self.assertFalse(old.exists())

    def test_dated_group_output_uses_selected_month_and_day_prefix(self):
        self.assertEqual(
            dated_group_output(Path("C:/result"), date(2026, 8, 31), "嫣然心水"),
            Path("C:/result") / "8.31-嫣然心水",
        )

    def test_elapsed_and_remaining_time_status_formatting(self):
        self.assertEqual(format_duration(222.9), "03:42")
        self.assertEqual(format_duration(3661), "01:01:01")
        self.assertEqual(
            format_processing_status(5, 10, 120),
            "正在处理：5 / 10｜已耗时 02:00｜预计剩余 02:00",
        )
        self.assertEqual(
            format_processing_status(0, 10, 0),
            "正在处理：0 / 10｜已耗时 00:00｜预计剩余：计算中…",
        )

    def test_capture_status_uses_one_fixed_group_json_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = capture_status_path(Path(directory), "嫣然心水")
            self.assertEqual(path.name, "嫣然心水抓取状态.json")
            payload = {
                "group_name": "嫣然心水",
                "date": "2026-08-31",
                "remarks": {"青苹果": {"status": "待复抓", "pending_message_ids": [12]}},
            }
            save_capture_status(path, payload)
            self.assertEqual(load_capture_status(path), payload)

    def test_capture_status_supports_every_group_with_its_own_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                capture_status_path(root, "新澳高手").name,
                "新澳高手抓取状态.json",
            )
            self.assertNotEqual(
                capture_status_path(root, "新澳高手"),
                capture_status_path(root, "嫣然心水"),
            )

    def test_retry_notes_exclude_completed_and_include_missing_and_new_labels(self):
        notes = {
            "天府统计": "天府统计",
            "天府": "天府统计",
            "青苹果": "青苹果",
            "新备注": "新备注",
        }
        status = {
            "remarks": {
                "天府统计": {"status": "已完成"},
                "青苹果": {"status": "未匹配"},
            }
        }
        self.assertEqual(
            notes_for_retry(notes, status),
            {"青苹果": "青苹果", "新备注": "新备注"},
        )

    def test_group_profile_creates_same_named_json_and_round_trips_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = load_group_settings(root)
            save_group_profile(root, settings, "", "澳洲六合彩", "https://t.me/example")
            settings["selected_group"] = "澳洲六合彩"
            settings["output_path"] = "C:/result"
            save_group_profile(root, settings, "澳洲六合彩", "澳洲六合彩", "@example")

            loaded = load_group_settings(root)
            self.assertEqual(
                loaded["groups"],
                [{"name": "澳洲六合彩", "address": "@example", "start_time": "00:00", "end_time": "23:59"}],
            )
            self.assertEqual(loaded["selected_group"], "澳洲六合彩")
            self.assertEqual(loaded["output_path"], "C:/result")
            notes_file = root / "群配置" / "澳洲六合彩.json"
            self.assertTrue(notes_file.is_file())
            self.assertEqual(json.loads(notes_file.read_text(encoding="utf-8")), {"keywords": []})

    def test_group_profile_rename_preserves_notes_and_delete_keeps_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = load_group_settings(root)
            save_group_profile(root, settings, "", "旧群名", "@old")
            old_notes = root / "群配置" / "旧群名.json"
            old_notes.write_text(json.dumps({"keywords": ["大小姐"]}, ensure_ascii=False), encoding="utf-8")

            save_group_profile(root, settings, "旧群名", "新群名", "@new")
            new_notes = root / "群配置" / "新群名.json"
            self.assertFalse(old_notes.exists())
            self.assertEqual(load_notes(new_notes), {"大小姐": "大小姐"})
            delete_group_profile(root, settings, "新群名")
            self.assertEqual(load_group_settings(root)["groups"], [])
            self.assertTrue(new_notes.exists())

    def test_purge_group_profile_removes_config_notes_results_status_and_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "结果"
            status = root / "outputs" / "抓取状态"
            logs = root / "outputs" / "运行日志"
            settings = load_group_settings(root)
            save_group_profile(root, settings, "", "要删的群", "@gone")
            save_group_profile(root, settings, "", "留下的群", "@keep")
            notes = root / "群配置" / "要删的群.json"
            notes.write_text(json.dumps({"keywords": ["大小姐"]}, ensure_ascii=False), encoding="utf-8")
            keep_notes = root / "群配置" / "留下的群.json"
            keep_notes.write_text(json.dumps({"keywords": ["小明"]}, ensure_ascii=False), encoding="utf-8")

            first = output / "10.17-要删的群"
            second = output / "10.18-要删的群"
            other = output / "10.18-留下的群"
            similar = output / "10.18-要删的群2"
            for folder in (first, second, other, similar):
                folder.mkdir(parents=True)
                (folder / "a.jpg").write_bytes(b"x")
            status.mkdir(parents=True)
            status_file = status / "要删的群抓取状态.json"
            status_file.write_text("{}", encoding="utf-8")
            keep_status = status / "留下的群抓取状态.json"
            keep_status.write_text("{}", encoding="utf-8")
            log_dir = logs / "要删的群"
            log_dir.mkdir(parents=True)
            (log_dir / "2026-10-18.log").write_text("日志", encoding="utf-8")
            keep_log = logs / "留下的群"
            keep_log.mkdir(parents=True)
            manual = status / "手动抓取日志.json"
            today = datetime.now(gui.CN_TZ).date()
            save_manual_capture_log(manual, {today.isoformat(): ["要删的群", "留下的群"]})

            targets = group_cleanup_targets(root, output, status, logs, "要删的群")
            labels = [label for label, _path in targets]
            self.assertIn("备注 JSON", labels)
            self.assertEqual(labels.count("结果文件夹"), 2)
            self.assertIn("抓取状态", labels)
            self.assertIn("运行日志目录", labels)

            removed, failed = purge_group_profile(
                root, settings, "要删的群", output_root=output, status_root=status, log_root=logs
            )
            self.assertEqual(failed, [])
            self.assertEqual(len(removed), 5)
            self.assertEqual([item["name"] for item in load_group_settings(root)["groups"]], ["留下的群"])
            self.assertFalse(notes.exists())
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertFalse(status_file.exists())
            self.assertFalse(log_dir.exists())
            self.assertTrue(keep_notes.exists())
            self.assertTrue(other.is_dir())
            self.assertTrue(similar.is_dir())
            self.assertTrue(keep_status.exists())
            self.assertTrue(keep_log.is_dir())
            self.assertEqual(load_manual_capture_log(manual, today), {today.isoformat(): ["留下的群"]})

    def test_purge_group_profile_refuses_unknown_group_and_keeps_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "结果"
            status = root / "outputs" / "抓取状态"
            logs = root / "outputs" / "运行日志"
            settings = load_group_settings(root)
            save_group_profile(root, settings, "", "存在的群", "@here")
            notes = root / "群配置" / "存在的群.json"
            notes.write_text(json.dumps({"keywords": []}, ensure_ascii=False), encoding="utf-8")
            folder = output / "10.18-存在的群"
            folder.mkdir(parents=True)

            with self.assertRaises(ValueError):
                purge_group_profile(
                    root, settings, "不存在的群", output_root=output, status_root=status, log_root=logs
                )
            self.assertTrue(notes.exists())
            self.assertTrue(folder.is_dir())
            self.assertEqual([item["name"] for item in load_group_settings(root)["groups"]], ["存在的群"])

    def test_group_cleanup_targets_ignores_missing_output_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                group_cleanup_targets(root, root / "不存在的结果目录", root / "状态", root / "日志", "空群"),
                [],
            )

    def test_purge_keeps_config_when_a_file_is_locked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "结果"
            status = root / "outputs" / "抓取状态"
            logs = root / "outputs" / "运行日志"
            settings = load_group_settings(root)
            save_group_profile(root, settings, "", "占用的群", "@locked")
            notes = root / "群配置" / "占用的群.json"
            notes.write_text(json.dumps({"keywords": []}, ensure_ascii=False), encoding="utf-8")
            folder = output / "10.18-占用的群"
            folder.mkdir(parents=True)
            (folder / "a.jpg").write_bytes(b"x")

            real_rmtree = gui.shutil.rmtree

            def locked_rmtree(path, *args, **kwargs):
                if Path(path) == folder:
                    raise PermissionError(32, "另一个程序正在使用此文件")
                return real_rmtree(path, *args, **kwargs)

            with patch.object(gui.shutil, "rmtree", side_effect=locked_rmtree):
                removed, failed = purge_group_profile(
                    root, settings, "占用的群", output_root=output, status_root=status, log_root=logs
                )
            # 文件删不掉就不能动配置，否则用户没法重试。
            self.assertEqual([path for path, _reason in failed], [folder])
            self.assertIn("PermissionError", failed[0][1])
            self.assertEqual([item["name"] for item in load_group_settings(root)["groups"]], ["占用的群"])
            self.assertTrue(notes.exists())
            self.assertTrue(folder.is_dir())
            self.assertIn(notes, removed)

            # 占用解除后可以正常重试
            removed, failed = purge_group_profile(
                root, settings, "占用的群", output_root=output, status_root=status, log_root=logs
            )
            self.assertEqual(failed, [])
            self.assertEqual(load_group_settings(root)["groups"], [])

    def test_purge_never_deletes_the_group_settings_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "结果"
            status = root / "outputs" / "抓取状态"
            logs = root / "outputs" / "运行日志"
            settings = load_group_settings(root)
            save_group_profile(root, settings, "", "普通群", "@normal")
            settings_file = root / "群配置" / "groups.json"
            before = settings_file.read_bytes()

            # 即使存在一个备注路径与 groups.json 相同的群，也不能把它列为删除目标。
            targets = group_cleanup_targets(root, output, status, logs, "普通群")
            self.assertNotIn(settings_file.resolve(), [path.resolve() for _label, path in targets])
            self.assertEqual(settings_file.read_bytes(), before)

    def test_resolve_output_root_rejects_empty_or_relative_path(self):
        with self.assertRaises(ValueError):
            gui.resolve_output_root("")
        with self.assertRaises(ValueError):
            gui.resolve_output_root("   ")
        with self.assertRaises(ValueError):
            gui.resolve_output_root("结果")
        self.assertEqual(gui.resolve_output_root('  "C:\\temp\\结果" '), Path("C:\\temp\\结果"))

    def test_group_name_validation_rejects_unsafe_windows_names_and_duplicates(self):
        self.assertEqual(validate_group_name("  澳洲六合彩  "), "澳洲六合彩")
        for name in ("", "A/B", "A.", "CON", "com1"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_group_name(name)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = load_group_settings(root)
            save_group_profile(root, settings, "", "测试群", "@one")
            with self.assertRaises(ValueError):
                save_group_profile(root, settings, "", "测试群", "@two")

    def test_group_profile_saves_its_own_time_range_and_validates_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = load_group_settings(root)
            save_group_profile(root, settings, "", "下午群", "@afternoon", "14:30", "21:30")
            self.assertEqual(load_group_settings(root)["groups"][0]["start_time"], "14:30")
            self.assertEqual(load_group_settings(root)["groups"][0]["end_time"], "21:30")
        self.assertEqual(parse_clock("08:05").strftime("%H:%M"), "08:05")
        for start, end in (("08.00", "21:00"), ("22:00", "21:00")):
            with self.subTest(start=start, end=end), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    save_group_profile(Path(directory), load_group_settings(Path(directory)), "", "错误时间", "@bad", start, end)

    def test_saved_credentials_are_encrypted_and_round_trip_for_this_windows_user(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "credentials.bin"
            save_saved_credentials(config_file, 12345, "secret-api-hash", "+8613800000000")
            encrypted = config_file.read_bytes()
            self.assertNotIn(b"secret-api-hash", encrypted)
            self.assertEqual(
                load_saved_credentials(config_file),
                (12345, "secret-api-hash", "+8613800000000"),
            )

    def test_matches_a_note_contained_inside_a_longer_caption(self):
        self.assertEqual(normalized("  陌生人　大小姐\n"), "陌生人 大小姐")
        notes = {normalized("大小姐"): "大小姐"}
        messages = [SimpleNamespace(id=1, raw_text="陌生人大小姐", grouped_id=None)]
        self.assertEqual(build_selection(messages, notes), {1: {"大小姐"}})

    def test_exact_caption_selects_the_whole_photo_album(self):
        notes = {normalized("陌生人大小姐"): "陌生人大小姐"}
        messages = [
            SimpleNamespace(id=10, raw_text="陌生人大小姐", grouped_id=88),
            SimpleNamespace(id=11, raw_text="", grouped_id=88),
            SimpleNamespace(id=12, raw_text="无关", grouped_id=None),
        ]
        self.assertEqual(
            build_selection(messages, notes),
            {10: {"陌生人大小姐"}, 11: {"陌生人大小姐"}},
        )

    def test_duplicate_captions_merge_intervening_captionless_split_album(self):
        notes = {normalized("天机阁杀料"): "天机阁杀料"}
        started = datetime(2026, 8, 29, 9, 57, tzinfo=timezone.utc)
        messages = [
            SimpleNamespace(id=1, raw_text="天机阁杀料", grouped_id=100, date=started),
            SimpleNamespace(id=2, raw_text="", grouped_id=100, date=started),
            SimpleNamespace(id=3, raw_text="", grouped_id=200, date=started + timedelta(seconds=10)),
            SimpleNamespace(id=4, raw_text="", grouped_id=200, date=started + timedelta(seconds=10)),
            SimpleNamespace(id=5, raw_text="天机阁杀料", grouped_id=300, date=started + timedelta(seconds=20)),
            SimpleNamespace(id=6, raw_text="", grouped_id=300, date=started + timedelta(seconds=20)),
        ]
        self.assertEqual(
            build_selection(messages, notes),
            {message.id: {"天机阁杀料"} for message in messages},
        )

    def test_duplicate_captions_merge_captionless_albums_across_the_selected_day(self):
        notes = {normalized("天机阁杀料"): "天机阁杀料"}
        started = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)
        messages = [
            SimpleNamespace(id=1, raw_text="天机阁杀料", grouped_id=100, date=started),
            SimpleNamespace(id=2, raw_text="", grouped_id=200, date=started + timedelta(minutes=5)),
            SimpleNamespace(id=3, raw_text="天机阁杀料", grouped_id=300, date=started + timedelta(minutes=10)),
        ]
        self.assertEqual(
            build_selection(messages, notes),
            {message.id: {"天机阁杀料"} for message in messages},
        )

    def test_乖乖团队_extends_two_similar_groups_and_stops_at_two(self):
        notes = {"乖乖团队": "乖乖团队"}
        messages = [
            SimpleNamespace(id=1, raw_text="乖乖团队", grouped_id=10, visual_type="blue"),
            SimpleNamespace(id=2, raw_text="", grouped_id=10, visual_type="blue"),
            SimpleNamespace(id=3, raw_text="", grouped_id=20, visual_type="blue"),
            SimpleNamespace(id=4, raw_text="", grouped_id=20, visual_type="blue"),
            SimpleNamespace(id=5, raw_text="", grouped_id=30, visual_type="blue"),
            SimpleNamespace(id=6, raw_text="", grouped_id=40, visual_type="blue"),
        ]
        compared = []

        def similarity(anchor, candidate):
            compared.append((anchor[0].id, candidate[0].id))
            return anchor[0].visual_type == candidate[0].visual_type

        self.assertEqual(
            build_download_selection(
                messages,
                notes,
                special_adjacent_labels={"乖乖团队"},
                group_similarity=similarity,
            ),
            {1: {"乖乖团队"}, 2: {"乖乖团队"}, 3: {"乖乖团队"}, 4: {"乖乖团队"}, 5: {"乖乖团队"}},
        )
        self.assertEqual(compared, [(1, 3), (3, 5)])

    def test_乖乖团队_does_not_classify_a_different_immediate_group_or_the_following_group(self):
        notes = {"乖乖团队": "乖乖团队"}
        messages = [
            SimpleNamespace(id=1, raw_text="乖乖团队", grouped_id=10, visual_type="blue"),
            SimpleNamespace(id=2, raw_text="", grouped_id=10, visual_type="blue"),
            SimpleNamespace(id=3, raw_text="", grouped_id=20, visual_type="red"),
            SimpleNamespace(id=4, raw_text="", grouped_id=30, visual_type="blue"),
        ]
        self.assertEqual(
            build_download_selection(
                messages,
                notes,
                special_adjacent_labels={"乖乖团队"},
                group_similarity=lambda anchor, candidate: anchor[0].visual_type == candidate[0].visual_type,
            ),
            {1: {"乖乖团队"}, 2: {"乖乖团队"}},
        )

    def test_乖乖团队_does_not_use_the_generic_multi_group_gap_merge(self):
        notes = {"乖乖团队": "乖乖团队"}
        messages = [
            SimpleNamespace(id=1, raw_text="乖乖团队", grouped_id=10, visual_type="blue"),
            SimpleNamespace(id=2, raw_text="", grouped_id=20, visual_type="blue"),
            SimpleNamespace(id=3, raw_text="", grouped_id=30, visual_type="red"),
            SimpleNamespace(id=4, raw_text="乖乖团队", grouped_id=40, visual_type="blue"),
        ]
        self.assertEqual(
            build_download_selection(
                messages,
                notes,
                special_adjacent_labels={"乖乖团队"},
                group_similarity=lambda anchor, candidate: anchor[0].visual_type == candidate[0].visual_type,
            ),
            {1: {"乖乖团队"}, 2: {"乖乖团队"}, 4: {"乖乖团队"}},
        )

    def test_image_group_similarity_accepts_matching_layout_color_and_rejects_different_type(self):
        def image_bytes(color, size=(720, 1280)):
            output = io.BytesIO()
            Image.new("RGB", size, color).save(output, format="JPEG")
            return output.getvalue()

        blue = image_bytes((70, 180, 230))
        near_blue = image_bytes((75, 175, 225))
        red = image_bytes((210, 40, 35))
        self.assertTrue(image_groups_are_similar([blue], [near_blue, blue]))
        self.assertTrue(image_groups_are_similar([blue], [blue, red, red]))
        self.assertFalse(image_groups_are_similar([blue], [red]))
        self.assertFalse(image_groups_are_similar([blue], [image_bytes((70, 180, 230), size=(1280, 300))]))

    def test_tianji_similarity_uses_its_own_color_and_aspect_limits(self):
        def payload(color, size=(100, 200)):
            buffer = io.BytesIO()
            Image.new("RGB", size, color).save(buffer, format="PNG")
            return buffer.getvalue()

        anchor = payload((254, 247, 223))
        partial_color = payload((254, 247, 80))
        stretched = payload((254, 247, 223), (125, 200))
        limits = dict(min_intersection=TIANJI_MIN_COLOR_INTERSECTION,
                      max_aspect_ratio=TIANJI_MAX_ASPECT_RATIO)
        for other in (partial_color, stretched):
            self.assertTrue(image_groups_are_similar([anchor], [other]))
            self.assertFalse(image_groups_are_similar([anchor], [other], **limits))
        self.assertTrue(image_groups_are_similar([partial_color, anchor], [stretched, anchor], **limits))

    def test_special_labels_use_exact_caption_matching(self):
        notes = {"战狼": "战狼"}
        messages = [
            SimpleNamespace(id=1, raw_text="⭐战狼", grouped_id=None),
            SimpleNamespace(id=2, raw_text="⭐战狼，赌神", grouped_id=None),
        ]
        self.assertEqual(
            build_download_selection(messages, notes, exact_labels={"战狼"}),
            {1: {"战狼"}},
        )

    def test_68_and_红人馆_keep_normal_keyword_matching(self):
        notes = {"68": "68", "红人馆": "红人馆"}
        messages = [
            SimpleNamespace(id=1, raw_text="红人馆 特别版", grouped_id=None),
            SimpleNamespace(id=2, raw_text="68资料汇总", grouped_id=None),
        ]
        self.assertEqual(
            build_download_selection(messages, notes, exact_labels={"战狼"}),
            {1: {"红人馆"}, 2: {"68"}},
        )

    def test_special_labels_check_both_immediate_neighbor_groups(self):
        notes = {"战狼": "战狼"}
        messages = [
            SimpleNamespace(id=1, raw_text="", grouped_id=10, visual_type="blue"),
            SimpleNamespace(id=2, raw_text="战狼", grouped_id=20, visual_type="blue"),
            SimpleNamespace(id=3, raw_text="", grouped_id=20, visual_type="blue"),
            SimpleNamespace(id=4, raw_text="", grouped_id=30, visual_type="blue"),
            SimpleNamespace(id=5, raw_text="", grouped_id=40, visual_type="red"),
        ]
        self.assertEqual(
            build_download_selection(
                messages,
                notes,
                special_adjacent_labels={"战狼"},
                bidirectional_adjacent_labels={"战狼"},
                group_similarity=lambda anchor, candidate: anchor[0].visual_type == candidate[0].visual_type,
            ),
            {1: {"战狼"}, 2: {"战狼"}, 3: {"战狼"}, 4: {"战狼"}},
        )

    def test_special_adjacent_label_can_be_added_to_group_already_matched_elsewhere(self):
        notes = {"战狼": "战狼", "红人馆": "红人馆"}
        messages = [
            SimpleNamespace(id=1, raw_text="战狼", grouped_id=10, visual_type="blue"),
            SimpleNamespace(id=2, raw_text="", grouped_id=20, visual_type="blue"),
        ]
        selection = add_similar_immediate_groups(
            messages,
            {1: {"战狼"}, 2: {"68"}},
            {"战狼"},
            group_similarity=lambda anchor, candidate: True,
            bidirectional_labels={"战狼"},
        )
        self.assertEqual(selection[2], {"68", "战狼"})

    def test_huangdaxian_preview_collection_includes_both_neighbors_for_special_labels(self):
        messages = [
            SimpleNamespace(id=1, raw_text="", grouped_id=10),
            SimpleNamespace(id=2, raw_text="战狼", grouped_id=20),
            SimpleNamespace(id=3, raw_text="", grouped_id=20),
            SimpleNamespace(id=4, raw_text="", grouped_id=30),
            SimpleNamespace(id=5, raw_text="红人馆", grouped_id=40),
            SimpleNamespace(id=6, raw_text="", grouped_id=40),
            SimpleNamespace(id=7, raw_text="", grouped_id=50),
        ]
        selection = {2: {"战狼"}, 3: {"战狼"}, 5: {"红人馆"}, 6: {"红人馆"}}
        self.assertEqual(
            collect_adjacent_preview_messages(
                messages,
                selection,
                {"战狼", "红人馆"},
                {"战狼", "红人馆"},
            ),
            {1, 2, 3, 4, 5, 6, 7},
        )

    def test_visual_preview_collection_covers_two_groups_on_each_side(self):
        messages = [SimpleNamespace(id=index, grouped_id=index * 10) for index in range(1, 8)]
        self.assertEqual(
            collect_adjacent_preview_messages(
                messages,
                {4: {"乖乖团队"}},
                {"乖乖团队"},
                {"乖乖团队"},
            ),
            {2, 3, 4, 5, 6},
        )

    def test_ocr_matches_special_label_from_any_recognized_text(self):
        class FakeOCR:
            def predict(self, image):
                return [{"rec_texts": ["澳门资料", "战狼团队原创"]}]

        payload = io.BytesIO()
        Image.new("RGB", (4, 4), (255, 255, 255)).save(payload, format="PNG")
        self.assertEqual(
            ocr_labels_from_payload(payload.getvalue(), {"战狼", "68", "红人馆", "香奈儿"}, FakeOCR()),
            {"战狼"},
        )

    def test_loads_json_notes_ignores_blanks_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            notes_file = Path(directory) / "notes.json"
            notes_file.write_text(
                json.dumps({"keywords": ["甲", "", "甲", "乙"]}, ensure_ascii=False), encoding="utf-8"
            )
            self.assertEqual(load_notes(notes_file), {"甲": "甲", "乙": "乙"})

    def test_note_aliases_match_one_shared_result_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            notes_file = Path(directory) / "notes.json"
            notes_file.write_text(
                json.dumps({"keywords": ["天府统计/天府"]}, ensure_ascii=False), encoding="utf-8"
            )
            notes = load_notes(notes_file)
        self.assertEqual(notes, {"天府统计": "天府统计", "天府": "天府统计"})
        messages = [
            SimpleNamespace(id=1, raw_text="天府统计", grouped_id=None),
            SimpleNamespace(id=2, raw_text="今日天府", grouped_id=None),
        ]
        self.assertEqual(
            build_selection(messages, notes),
            {1: {"天府统计"}, 2: {"天府统计"}},
        )

    def test_36_code_aliases_share_canonical_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            notes_file = Path(directory) / "notes.json"
            notes_file.write_text(
                json.dumps({"keywords": ["36码围特/36码 围特"]}, ensure_ascii=False), encoding="utf-8"
            )
            self.assertEqual(
                load_notes(notes_file),
                {"36码围特": "36码围特", "36码 围特": "36码围特"},
            )

    def test_empty_notes_select_every_image_for_all_images_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            notes_file = Path(directory) / "empty.json"
            notes_file.write_text(json.dumps({"keywords": []}), encoding="utf-8")
            notes = load_notes(notes_file, allow_empty=True)
        messages = [
            SimpleNamespace(id=1, raw_text="", grouped_id=None),
            SimpleNamespace(id=2, raw_text="任意备注", grouped_id=99),
        ]
        self.assertEqual(notes, {})
        self.assertEqual(build_download_selection(messages, notes), {1: {"全部图片"}, 2: {"全部图片"}})

    def test_legacy_group_txt_migrates_to_json_without_losing_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "群配置"
            config_dir.mkdir()
            (config_dir / "groups.json").write_text(
                json.dumps(
                    {
                        "groups": [
                            {
                                "name": "旧群",
                                "address": "@old",
                                "start_time": "00:00",
                                "end_time": "23:59",
                            }
                        ],
                        "selected_group": "旧群",
                        "output_path": "C:/result",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            legacy = config_dir / "旧群.txt"
            legacy.write_text("大小姐\n天府统计/天府\n", encoding="utf-8-sig")

            load_group_settings(root)

            migrated = config_dir / "旧群.json"
            self.assertFalse(legacy.exists())
            self.assertEqual(
                json.loads(migrated.read_text(encoding="utf-8")),
                {"keywords": ["大小姐", "天府统计/天府"]},
            )
            self.assertEqual(
                load_notes(migrated),
                {"大小姐": "大小姐", "天府统计": "天府统计", "天府": "天府统计"},
            )

    def test_rejects_invalid_date_and_sanitizes_windows_folder_name(self):
        with self.assertRaises(ValueError):
            parse_day("2026/08/29")
        self.assertEqual(safe_folder_name('A/B:*?'), "A_B___")


class GuiFlowTests(unittest.TestCase):
    def setUp(self):
        self.app_directory = tempfile.TemporaryDirectory()
        self.root = Tk()
        self.root.withdraw()
        self.app = TelegramDownloaderApp(
            self.root,
            app_data=Path(self.app_directory.name),
            program_root=Path(self.app_directory.name) / "program",
            status_root=Path(self.app_directory.name) / "status",
            show_account_dialog=False,
        )
        self.root.update()
        self.app.api_id.set("12345")
        self.app.api_hash.set("secret-hash")
        self.app.phone.set("+8613800000000")
        self.account_id = "0" * 32
        save_saved_credentials(
            account_directory(Path(self.app_directory.name), self.account_id) / "credentials.bin",
            12345, "secret-hash", "+8613800000000",
        )
        save_account_profile(Path(self.app_directory.name),
                             {"id": self.account_id, "name": "测试号", "user_id": 7})
        self.app.refresh_account_profiles()
        self.app.select_account(self.account_id)

    def tearDown(self):
        self.root.update()
        self.root.destroy()
        self.app_directory.cleanup()

    def test_window_builds_and_validates_credentials(self):
        self.assertEqual(self.root.title(), "登录飞机提取图片 v5.4.8")
        self.assertEqual(
            self.app.output_path.get(),
            r"C:\Users\Administrator\Desktop\每天工具\飞机抓图\结果",
        )
        self.assertEqual(self.app.credentials(), (12345, "secret-hash", "+8613800000000"))
        self.app.api_id.set("not-a-number")
        with self.assertRaises(ValueError):
            self.app.credentials()

    def test_auto_capture_page_is_reachable_from_account_settings_and_saves_group_rule(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "自动群", "@auto")
        self.app.reload_group_profiles("自动群")
        self.app.open_login_settings()
        self.root.update()
        self.assertTrue(self.app.auto_capture_button.winfo_exists())
        self.app.open_auto_capture_settings()
        self.root.update()
        dialog = self.app.auto_capture_settings_dialog
        self.assertEqual(dialog.window.title(), "自动抓取设置")
        item_id = dialog.tree.get_children()[0]
        dialog.tree.selection_set(item_id)
        self.root.update()
        dialog.enabled.set(True)
        dialog.first_time.set("18:00")
        dialog.second_time.set("22:30")
        dialog.min_images.set("1000")
        dialog.save_item()
        saved = load_group_settings(self.app.program_root)["groups"][0]["auto_capture"]
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["first_time"], "18:00")
        self.assertEqual(saved["second_time"], "22:30")
        self.assertEqual(saved["min_images"], 1000)
        self.app.auto_capture_settings_dialog.window.destroy()
        self.app.login_settings_window.destroy()

    def test_delete_account_button_removes_selected_account_after_confirmation(self):
        app_data = Path(self.app_directory.name)
        new = create_account_profile(app_data, "待删除号", 456, "new-secret", "+8613900000000")
        folder = account_directory(app_data, new["id"])
        self.app.open_login_settings()
        self.app.select_account(new["id"])
        self.root.update()
        self.assertTrue(self.app.delete_account_button.winfo_exists())
        self.assertEqual(str(self.app.delete_account_button.cget("state")), "normal")
        with patch("telegram_caption_downloader_gui.messagebox.askyesno", return_value=True) as confirm:
            self.app.delete_account()
        self.root.update()
        confirm.assert_called_once()
        self.assertFalse(folder.exists())
        self.assertEqual([profile["id"] for profile in self.app.account_profiles], [self.account_id])
        self.assertEqual(self.app.selected_account_id, "")
        self.assertEqual(str(self.app.delete_account_button.cget("state")), "disabled")
        self.app.login_settings_window.destroy()

    def test_delete_account_is_blocked_for_the_logged_in_account(self):
        app_data = Path(self.app_directory.name)
        new = create_account_profile(app_data, "待删除号", 456, "new-secret", "+8613900000000")
        folder = account_directory(app_data, new["id"])
        self.app.open_login_settings()
        self.app.select_account(new["id"])
        self.app.logged_in = True
        self.app.active_account_id = new["id"]
        self.root.update()
        with patch("telegram_caption_downloader_gui.messagebox.showwarning") as warn:
            self.app.delete_account()
        self.root.update()
        warn.assert_called_once()
        self.assertTrue(folder.exists())
        self.assertEqual(len(self.app.account_profiles), 2)
        self.app.login_settings_window.destroy()

    def test_auto_capture_scheduler_records_first_attempt_and_dispatches_group(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "自动群", "@auto")
        self.app.reload_group_profiles("自动群")
        self.app.settings["groups"][0]["auto_capture"] = normalize_auto_capture_settings({
            "enabled": True, "first_time": "18:00", "second_time": "22:30", "min_images": 1000,
        })
        self.app.logged_in = True
        self.app.selected_account_id = self.app.active_account_id = self.account_id
        self.app.active_user_id = 7
        with patch.object(self.app, "start_download") as start_download:
            self.app._check_auto_capture_schedule(datetime(2026, 10, 3, 18, 0, tzinfo=timezone(timedelta(hours=8))))
        start_download.assert_called_once_with(
            group_name_override="自动群", target_day_override=date(2026, 10, 3), auto_attempt=1, auto_min_images=1000,
        )
        self.assertEqual(self.app.settings["groups"][0]["auto_capture"]["first_attempt_date"], "2026-10-03")

    def test_auto_capture_scheduler_skips_group_recorded_in_manual_capture_json_log(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "自动群", "@auto")
        self.app.reload_group_profiles("自动群")
        profile = self.app.settings["groups"][0]
        profile["auto_capture"] = normalize_auto_capture_settings({
            "enabled": True, "first_time": "18:00", "second_time": "22:30", "min_images": 1000,
        })
        today = datetime.now(gui.CN_TZ).date()
        record_manual_capture(manual_capture_log_path(self.app.status_root), "自动群", today)
        self.app.logged_in = True
        self.app.selected_account_id = self.app.active_account_id = self.account_id
        self.app.active_user_id = 7
        with patch.object(self.app, "start_download") as start_download:
            self.app._check_auto_capture_schedule(
                datetime(today.year, today.month, today.day, 18, 0, tzinfo=gui.CN_TZ)
            )
        start_download.assert_not_called()
        self.assertEqual(profile["auto_capture"]["first_attempt_date"], "")
        profile["auto_capture"]["first_attempt_date"] = today.isoformat()
        gui.save_auto_capture_settings(self.app.program_root, self.app.settings, profile, profile["auto_capture"])
        self.app.settings = load_group_settings(self.app.program_root)
        second_time = datetime(today.year, today.month, today.day, 22, 30, tzinfo=gui.CN_TZ)
        with patch.object(self.app, "_begin_auto_capture") as begin:
            self.app._check_auto_capture_schedule(second_time)
            begin.assert_not_called()
            self.app._check_auto_capture_schedule(second_time + timedelta(days=1))
            self.assertEqual(begin.call_count, 1)
            # 已过第二次时间才启动：直接按第 2 次只跑一轮，不再连跑两次全量扫描。
            self.assertEqual(begin.call_args.args[2:], (2, today + timedelta(days=1)))

    def test_auto_capture_keeps_manual_skip_when_expired_log_cleanup_cannot_write(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "自动群", "@auto")
        self.app.reload_group_profiles("自动群")
        self.app.settings["groups"][0]["auto_capture"] = normalize_auto_capture_settings(dict(
            enabled=True, first_time="18:00", second_time="22:30", min_images=1000,
        ))
        today = datetime.now(gui.CN_TZ).date()
        path = manual_capture_log_path(self.app.status_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"days": {
            today.isoformat(): ["自动群"],
            (today - timedelta(days=7)).isoformat(): ["旧群"],
        }}, ensure_ascii=False), encoding="utf-8")
        original = path.read_bytes()
        self.app.logged_in = True
        self.app.selected_account_id = self.app.active_account_id = self.account_id
        with patch.object(gui.os, "replace", side_effect=PermissionError("locked")), \
             patch.object(self.app, "_begin_auto_capture") as begin, \
             patch.object(self.app, "log") as log:
            self.app._check_auto_capture_schedule(datetime(today.year, today.month, today.day, 18, tzinfo=gui.CN_TZ))
            begin.assert_not_called()
            self.assertTrue(any("过期记录清理失败" in call.args[0] for call in log.call_args_list))
        self.assertEqual(path.read_bytes(), original)

    def test_auto_capture_rule_edit_preserves_today_attempts_and_failed_save_rolls_back(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "自动群", "@auto")
        self.app.reload_group_profiles("自动群")
        profile = self.app.settings["groups"][0]
        profile["auto_capture"] = normalize_auto_capture_settings({
            "enabled": True, "first_time": "18:00", "second_time": "22:00", "min_images": 1000,
            "first_attempt_date": "2026-10-03", "second_attempt_date": "2026-10-03",
            "last_discovered_date": "2026-10-03", "last_discovered_images": 193,
        })
        self.app.open_auto_capture_settings()
        self.root.update()
        dialog = self.app.auto_capture_settings_dialog
        dialog.min_images.set("900")
        dialog.save_item()
        saved = load_group_settings(self.app.program_root)["groups"][0]["auto_capture"]
        self.assertEqual(saved["first_attempt_date"], "2026-10-03")
        self.assertEqual(saved["second_attempt_date"], "2026-10-03")
        self.assertEqual(saved["last_discovered_images"], 193)
        self.root.update()
        dialog.min_images.set("800")
        before = dict(profile["auto_capture"])
        with patch("telegram_caption_downloader_gui._write_group_settings", side_effect=OSError("disk full")), \
             patch("telegram_caption_downloader_gui.messagebox.showwarning"):
            dialog.save_item()
        self.assertEqual(profile["auto_capture"], before)
        self.app.set_busy(True)
        dialog.min_images.set("700")
        dialog.save_item()
        self.assertEqual(profile["auto_capture"], before)
        self.app.set_busy(False)

    def test_auto_capture_scheduler_skips_busy_unlogged_and_workflow_runs(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "自动群", "@auto")
        self.app.settings["groups"][0]["auto_capture"] = normalize_auto_capture_settings({
            "enabled": True, "first_time": "18:00", "second_time": "22:00", "min_images": 1000,
        })
        now = datetime(2026, 10, 3, 18, 0, tzinfo=timezone(timedelta(hours=8)))
        with patch.object(self.app, "start_download") as worker:
            self.app._check_auto_capture_schedule(now)
            self.app.logged_in = True
            self.app.selected_account_id = self.app.active_account_id = self.account_id
            self.app._busy = True
            self.app._check_auto_capture_schedule(now)
            self.app._busy = False
            self.app._workflow_mode = True
            self.app._check_auto_capture_schedule(now)
        worker.assert_not_called()
        self.assertEqual(self.app.settings["groups"][0]["auto_capture"]["first_attempt_date"], "")
        self.app._workflow_mode = False

    def test_auto_capture_threshold_blocks_extraction_before_writing_results(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "阈值群", "@threshold")
        self.app.reload_group_profiles("阈值群")
        (self.app.program_root / "群配置" / "阈值群.json").write_text(
            '{"keywords":["战狼"]}', encoding="utf-8"
        )
        self.app.output_path.set(str(root / "results"))
        self.app.logged_in = True
        self.app.selected_account_id = self.app.active_account_id = self.account_id
        self.app.active_user_id = 7

        message = SimpleNamespace(
            id=1,
            raw_text="战狼",
            grouped_id=None,
            date=datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc),
            file=SimpleNamespace(mime_type="image/jpeg", ext=".jpg"),
            photo=True,
        )

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, _entity, **_kwargs):
                return iter([message])

        results = []
        self.app.run_worker = lambda operation, success, on_failure=None: results.append(operation())
        with patch("telethon.sync.TelegramClient", Client):
            self.app.start_download(
                group_name_override="阈值群",
                target_day_override=date(2026, 10, 3),
                auto_attempt=1,
                auto_min_images=2,
            )
        self.app._busy = False
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].threshold_blocked)
        self.assertEqual(results[0].discovered_images, 1)
        self.assertFalse((root / "results" / "10.3-阈值群").exists())
        self.assertFalse(capture_status_path(self.app.status_root, "阈值群").exists())

    def test_auto_capture_retries_once_after_low_first_count_and_stops_after_pass(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "两次群", "@twice")
        self.app.reload_group_profiles("两次群")
        profile = self.app.settings["groups"][0]
        profile["auto_capture"] = normalize_auto_capture_settings({
            "enabled": True, "first_time": "18:00", "second_time": "22:00", "min_images": 1000,
        })
        self.app.logged_in = True
        self.app.selected_account_id = self.app.active_account_id = self.account_id
        calls = []
        self.app.start_download = lambda **kwargs: calls.append(kwargs)
        tz = timezone(timedelta(hours=8))
        first_now = datetime(2026, 10, 3, 18, 0, tzinfo=tz)
        second_now = datetime(2026, 10, 3, 22, 0, tzinfo=tz)
        self.app._check_auto_capture_schedule(first_now)
        self.assertEqual(calls[0]["auto_attempt"], 1)
        self.app._finish_auto_capture(SimpleNamespace(discovered_images=193, threshold_blocked=True))
        self.app._check_auto_capture_schedule(second_now)
        self.assertEqual(calls[1]["auto_attempt"], 2)
        self.app._finish_auto_capture(SimpleNamespace(
            discovered_images=1035, threshold_blocked=False, failed_images=0, unmatched=0, downloaded=1035,
        ))
        self.app._check_auto_capture_schedule(datetime(2026, 10, 3, 23, 0, tzinfo=tz))
        self.assertEqual(len(calls), 2)
        saved = load_group_settings(self.app.program_root)["groups"][0]["auto_capture"]
        self.assertEqual(saved["last_discovered_images"], 1035)
        self.assertEqual(saved["captured_date"], "2026-10-03")

    def test_thread_stack_dump_reports_every_thread(self):
        dump = thread_stack_dump()
        self.assertIn("MainThread", dump)
        self.assertIn("test_thread_stack_dump_reports_every_thread", dump)

    def test_open_with_shell_never_runs_on_the_ui_thread(self):
        calls = []

        def record(target):
            calls.append((target, threading.current_thread() is threading.main_thread()))

        with patch("telegram_caption_downloader_gui.os.startfile", side_effect=record):
            self.app._open_with_shell("C:/tmp/example.json")
            for _ in range(100):
                if calls:
                    break
                time.sleep(0.02)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "C:/tmp/example.json")
        self.assertFalse(calls[0][1], "os.startfile 跑在界面线程会卡死窗口")

    def test_freeze_dump_is_written_to_the_runtime_log(self):
        self.app._freeze_dump(7.0)
        logs = list((Path(self.app_directory.name) / "program" / "运行日志").rglob("*.log"))
        self.assertTrue(logs)
        content = max(logs, key=lambda path: path.stat().st_mtime).read_text(encoding="utf-8")
        self.assertIn("界面线程无响应", content)
        self.assertIn("线程堆栈", content)

    def test_group_click_while_busy_does_not_restorm_selection_events(self):
        self.app._busy = True
        self.app.group_list.insert("", "end", iid="甲", values=("", "甲"))
        self.app.group_list.insert("", "end", iid="乙", values=("", "乙"))
        self.app.chat.set("甲")
        self.app.group_list.selection_set("甲")
        calls = []
        real_select = self.app.group_list.selection_set

        def counted(items):
            calls.append(items)
            if len(calls) > 5:  # 真出回归时不要让测试进程挂死
                return None
            return real_select(items)

        with patch.object(self.app.group_list, "selection_set", side_effect=counted):
            self.app.group_list.selection_set("乙")
            for _ in range(5):
                self.root.update()
        self.assertEqual(self.app.group_list.selection(), ("甲",))
        self.assertLessEqual(len(calls), 2)

    def test_main_window_opens_centered_on_screen(self):
        self.root.deiconify()
        self.root.update()
        window_center = (
            self.root.winfo_x() + self.root.winfo_width() / 2,
            self.root.winfo_y() + self.root.winfo_height() / 2,
        )
        screen_center = (
            self.root.winfo_screenwidth() / 2,
            self.root.winfo_screenheight() / 2,
        )
        self.assertAlmostEqual(window_center[0], screen_center[0], delta=3)
        self.assertAlmostEqual(window_center[1], screen_center[1], delta=3)

    def test_dark_workspace_layout_and_login_settings_entry(self):
        self.assertEqual(self.root.cget("background").lower(), "#161719")
        self.assertEqual(self.app.group_list.winfo_manager(), "pack")
        self.assertEqual(self.app.group_button.cget("text"), "群组设置")
        self.assertEqual(self.app.status_button.cget("text"), "查看抓取状态")
        self.assertEqual(self.app.retry_button.cget("text"), "复抓")
        self.assertEqual(self.app.open_button.cget("text"), "打开结果")
        self.assertEqual(self.app.start_button.cget("style"), "Accent.TButton")
        self.assertEqual(self.app.clear_button.cget("style"), "Danger.TButton")
        self.assertEqual(self.app.log_box.cget("background").lower(), "#111214")
        body_font = Font(root=self.root, font=self.app.style.lookup(".", "font"))
        log_font = Font(root=self.root, font=self.app.log_box.cget("font"))
        entry_font = Font(root=self.root, font=self.app.style.lookup("Dark.TEntry", "font"))
        combo_font = Font(root=self.root, font=self.app.style.lookup("Dark.TCombobox", "font"))
        dialog_entry_font = Font(root=self.root, font=self.app.style.lookup("TEntry", "font"))
        form_label_font = Font(root=self.root, font=self.app.style.lookup("Card.TLabel", "font"))
        checkbox_font = Font(root=self.root, font=self.app.style.lookup("TCheckbutton", "font"))
        self.assertGreaterEqual(body_font.actual("size"), 12)
        self.assertGreaterEqual(log_font.actual("size"), 11)
        self.assertGreaterEqual(entry_font.actual("size"), 13)
        self.assertGreaterEqual(combo_font.actual("size"), 13)
        self.assertGreaterEqual(dialog_entry_font.actual("size"), 13)
        self.assertGreaterEqual(form_label_font.actual("size"), 13)
        self.assertGreaterEqual(checkbox_font.actual("size"), 13)
        self.assertFalse(hasattr(self.app, "progress"))

        self.app.open_login_settings()
        self.root.update()
        self.assertTrue(self.app.login_settings_window.winfo_exists())
        self.assertEqual(self.app.login_settings_window.title(), "选择 Telegram 账号")
        self.app.login_settings_window.destroy()

    def test_group_list_marks_captured_groups_red_and_follows_selection(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "已抓群", "@a")
        save_group_profile(self.app.program_root, self.app.settings, "", "未抓群", "@b")
        self.app.reload_group_profiles("未抓群")
        output = Path(self.app_directory.name) / "result"
        self.app.output_path.set(str(output))
        self.app.day.set("2026-09-05")
        dated_group_output(output, date(2026, 9, 5), "已抓群").mkdir(parents=True)
        self.app.update_first_capture_status()
        self.assertEqual(list(self.app.group_list.get_children()), ["已抓群", "未抓群"])
        self.assertEqual(self.app.group_list.item("已抓群", "tags"), ("captured",))
        self.assertFalse(self.app.group_list.item("未抓群", "tags"))
        self.assertEqual(str(self.app.group_list.tag_configure("captured", "foreground")), "#E0715B")
        self.assertEqual(self.app.group_list.selection(), ("未抓群",))
        self.app.group_list.selection_set("已抓群")
        self.app.on_group_list_selected()
        self.assertEqual(self.app.chat.get(), "已抓群")
        self.assertEqual(self.app.first_capture_status.get(), "今日已首抓")

    def test_per_link_notes_match_exclusively_and_keep_same_message_ids(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "测试群", "@a | @b")
        self.app.reload_group_profiles("测试群")
        (self.app.program_root / "群配置" / "测试群.json").write_text(
            json.dumps({"keywords": ["战狼"], "links": {"@b": ["综合"]}}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")

        class Message:
            grouped_id = None
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True

            def __init__(self, message_id, caption):
                self.id, self.raw_text = message_id, caption

            async def download_media(self, file):
                Path(file).write_bytes(b"image")
                return file

        messages_by_source = {
            "@a": [Message(1000, "战狼"), Message(1001, "综合")],
            "@b": [Message(1000, "综合"), Message(1001, "战狼")],
        }

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, entity, **_kwargs):
                return iter(messages_by_source[entity])

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
        ):
            self.app.start_download()
        group_dir = root / "results" / "9.2-测试群"
        self.assertEqual(len(list((group_dir / "战狼").glob("*.jpg"))), 1)
        self.assertEqual(len(list((group_dir / "综合").glob("*.jpg"))), 1)
        status = load_capture_status(capture_status_path(self.app.status_root, "测试群"))
        self.assertEqual(status["remarks"]["战狼"]["status"], "已完成")
        self.assertEqual(status["remarks"]["战狼"]["image_messages"], [{"source": "@a", "id": 1000}])
        self.assertEqual(status["remarks"]["综合"]["status"], "已完成")
        self.assertEqual(status["remarks"]["综合"]["image_messages"], [{"source": "@b", "id": 1000}])
        self.assertEqual((group_dir / "未匹配备注.txt").read_text(encoding="utf-8-sig"), "")

    def test_group_list_click_switches_group_via_real_event(self):
        self.assertTrue(self.app.group_list.bind("<<TreeviewSelect>>"))
        self.assertFalse(self.app.group_list.bind("<<ListboxSelect>>"))
        save_group_profile(self.app.program_root, self.app.settings, "", "切换群", "@switch")
        save_group_profile(self.app.program_root, self.app.settings, "", "目标群", "@target")
        self.app.reload_group_profiles("切换群")
        self.assertEqual(self.app.chat.get(), "切换群")
        self.app.group_list.selection_set("目标群")
        self.app.group_list.event_generate("<<TreeviewSelect>>")
        self.root.update()
        self.assertEqual(self.app.chat.get(), "目标群")
        self.assertEqual(
            self.app.notes_path.get(),
            str(self.app.program_root / "群配置" / "目标群.json"),
        )
        self.assertEqual(self.app.settings["selected_group"], "目标群")

    def test_window_preset_defaults_follow_screen_width(self):
        with patch("telegram_caption_downloader_gui.load_window_preset", return_value=""):
            with patch.object(self.root, "winfo_screenwidth", return_value=2048):
                self.assertEqual(self.app._initial_window_preset(), "2k")
            with patch.object(self.root, "winfo_screenwidth", return_value=1920):
                self.assertEqual(self.app._initial_window_preset(), "1080")
        with patch("telegram_caption_downloader_gui.load_window_preset", return_value="1080"):
            self.assertEqual(self.app._initial_window_preset(), "1080")

    def test_window_preset_switch_saves_and_restarts_only_when_changed(self):
        root = Path(self.app_directory.name)
        self.app.window_preset = "1080"
        with (
            patch("telegram_caption_downloader_gui.messagebox.showinfo") as info,
            patch.object(self.app, "restart_application") as restart,
        ):
            self.app.choose_window_preset("1080")
            restart.assert_not_called()
            info.assert_called_once()
            self.app.choose_window_preset("2k")
            restart.assert_called_once()
        self.assertEqual(load_window_preset(root), "2k")
        with (
            patch("telegram_caption_downloader_gui.messagebox.showwarning") as warning,
            patch.object(self.app, "restart_application") as restart,
        ):
            self.app.set_busy(True)
            self.app.choose_window_preset("1080")
            self.app.set_busy(False)
        warning.assert_called_once()
        restart.assert_not_called()
        self.assertEqual(load_window_preset(root), "2k")

    def test_login_client_uses_bounded_connection_timeout(self):
        captured = {}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)

            def disconnect(self):
                pass

        with patch("telethon.sync.TelegramClient", FakeClient):
            with logged_telegram_client("unused", 123, "SECRET", lambda text: None):
                pass
        self.assertEqual(captured.get("timeout"), 15)
        self.assertEqual(captured.get("connection_retries"), 2)

    def test_worker_thread_callbacks_are_delivered_by_queue_pump(self):
        results = []

        def worker():
            self.app._post_to_main(lambda: results.append("worker"))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(results, [])
        self.app._pump_ui_callbacks()
        self.assertEqual(results, ["worker"])
        self.app._post_to_main(lambda: results.append("main"))
        self.root.update()
        self.assertEqual(results, ["worker", "main"])

    def test_login_watchdog_recovers_busy_ui_once(self):
        self.app.set_busy(True, "准备中…")
        self.app._login_token = "token-a"
        self.app._login_in_progress = True
        with patch.object(self.app, "open_login_settings") as opener:
            self.app._login_watchdog("token-stale")
            self.assertIn("准备中", self.app.task_status.get())
            # 到达重排上限后强制恢复界面
            self.app._login_watchdog("token-a", gui.LOGIN_WATCHDOG_MAX_CHECKS)
        self.assertEqual(self.app._login_token, "")
        self.assertIn("登录超时", self.app.task_status.get())
        self.assertFalse(self.app._busy)
        self.assertFalse(self.app._login_in_progress)
        opener.assert_called_once()

    def test_login_watchdog_ignores_finished_login(self):
        self.app.set_busy(True, "导入中…")
        self.app._login_token = "token-a"
        self.app._login_in_progress = False
        try:
            with patch.object(self.app, "open_login_settings") as opener:
                self.app._login_watchdog("token-a")
            # 登录已结束（token 已清）时，看门狗不能去解锁别的任务
            self.assertTrue(self.app._busy)
            opener.assert_not_called()
        finally:
            self.app.set_busy(False)

    def test_login_watchdog_keeps_ui_locked_while_login_worker_alive(self):
        self.app.set_busy(True, "准备中…")
        self.app._login_token = "token-a"
        self.app._login_in_progress = True
        scheduled = []
        original_after = self.app.root.after
        self.app.root.after = lambda delay, callback=None, *args: (scheduled.append(delay), "id")[1]
        try:
            with patch.object(self.app, "open_login_settings") as opener:
                self.app._login_watchdog("token-a")
            self.assertTrue(self.app._busy)
            opener.assert_not_called()
            self.assertIn(60000, scheduled)
            self.assertEqual(self.app._login_token, "token-a")
        finally:
            self.app.root.after = original_after
            self.app._login_in_progress = False
            self.app.set_busy(False)

    def test_login_failure_opens_account_window(self):
        original_after = self.app.root.after
        self.app.root.after = lambda _delay, callback: callback()

        class ImmediateThread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

        with (
            patch("telegram_caption_downloader_gui.threading.Thread", ImmediateThread),
            patch("telegram_caption_downloader_gui.messagebox.showerror"),
            patch.object(self.app, "open_login_settings") as opener,
        ):
            self.app.run_worker(
                lambda: (_ for _ in ()).throw(RuntimeError("test failure")),
                lambda _result: None,
                on_failure=self.app.open_login_settings,
            )
        self.app.root.after = original_after
        opener.assert_called_once()

    def test_login_dialog_highlights_current_window_preset(self):
        self.app.window_preset = "2k"
        self.app.open_login_settings()
        self.root.update()
        try:
            self.assertEqual(str(self.app._preset_buttons["2k"].cget("style")), "PresetActive.TButton")
            self.assertEqual(str(self.app._preset_buttons["1080"].cget("style")), "Preset.TButton")
            self.app.window_preset = "1080"
            self.app._refresh_preset_buttons()
            self.assertEqual(str(self.app._preset_buttons["1080"].cget("style")), "PresetActive.TButton")
        finally:
            self.app.login_settings_window.destroy()

    def test_settings_windows_center_on_main_and_keep_button_text_visible(self):
        self.root.deiconify()
        self.root.geometry("1180x760+100+60")
        self.root.update()
        main_center = (
            self.root.winfo_rootx() + self.root.winfo_width() / 2,
            self.root.winfo_rooty() + self.root.winfo_height() / 2,
        )

        group_dialog = GroupSettingsDialog(self.app)
        try:
            self.root.update()
            group_center = (
                group_dialog.window.winfo_rootx() + group_dialog.window.winfo_width() / 2,
                group_dialog.window.winfo_rooty() + group_dialog.window.winfo_height() / 2,
            )
            self.assertAlmostEqual(group_center[0], main_center[0], delta=3)
            self.assertAlmostEqual(group_center[1], main_center[1], delta=3)
        finally:
            group_dialog.window.destroy()

        self.app.open_login_settings()
        self.root.update()
        login_window = self.app.login_settings_window
        login_center = (
            login_window.winfo_rootx() + login_window.winfo_width() / 2,
            login_window.winfo_rooty() + login_window.winfo_height() / 2,
        )
        self.assertAlmostEqual(login_center[0], main_center[0], delta=3)
        self.assertAlmostEqual(login_center[1], main_center[1], delta=3)
        self.assertGreaterEqual(
            self.app.login_submit_button.winfo_height(),
            self.app.login_submit_button.winfo_reqheight(),
        )
        self.assertGreaterEqual(
            self.app.login_close_button.winfo_height(),
            self.app.login_close_button.winfo_reqheight(),
        )
        login_window.destroy()

    def test_all_form_fields_use_an_explicit_readable_font(self):
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        def assert_form_fonts(window):
            fields = [
                widget
                for widget in descendants(window)
                if widget.winfo_class() in {"TEntry", "TCombobox"}
            ]
            self.assertTrue(fields)
            for field in fields:
                actual = Font(root=self.root, font=field.cget("font")).actual("size")
                self.assertEqual(actual, 13, field.winfo_class())

        assert_form_fonts(self.root)

        group_dialog = GroupSettingsDialog(self.app)
        try:
            assert_form_fonts(group_dialog.window)
        finally:
            group_dialog.window.destroy()

        self.app.open_login_settings()
        try:
            assert_form_fonts(self.app.login_settings_window)
        finally:
            self.app.login_settings_window.destroy()

    def test_file_and_folder_pickers_update_fields(self):
        with patch("telegram_caption_downloader_gui.filedialog.askdirectory", return_value="C:/result"):
            self.app.choose_output()
        self.assertEqual(self.app.output_path.get(), "C:/result")
        self.assertEqual(load_group_settings(self.app.program_root)["output_path"], "C:/result")

    def test_group_selection_resolves_address_and_same_named_notes(self):
        save_group_profile(
            self.app.program_root,
            self.app.settings,
            "",
            "我的群",
            "@group_address",
            "14:30",
            "21:30",
        )
        self.app.reload_group_profiles("我的群")
        notes = self.app.program_root / "群配置" / "我的群.json"
        self.assertEqual(self.app.notes_path.get(), str(notes))
        self.assertEqual(self.app.selected_group_address(), "@group_address")
        self.assertEqual(self.app.time_range.get(), "14:30 ～ 21:30")

    def test_yanran_retry_button_uses_only_unmatched_result_notes(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "嫣然心水", "@yanran")
        save_group_profile(self.app.program_root, self.app.settings, "", "其他群", "@other")
        self.app.reload_group_profiles("嫣然心水")
        output = Path(self.app_directory.name) / "result"
        self.app.day.set("2026-08-29")
        unmatched = output / "8.29-嫣然心水" / "未匹配备注.txt"
        unmatched.parent.mkdir(parents=True)
        unmatched.write_text("烟草味\n辣椒炒肉\n", encoding="utf-8-sig")
        self.app.output_path.set(str(output))

        self.assertTrue(self.app.retry_button.winfo_manager())
        with patch.object(self.app, "start_download") as start_download:
            self.app.retry_unmatched()
        start_download.assert_called_once_with(
            notes_override={"烟草味": "烟草味", "辣椒炒肉": "辣椒炒肉"},
            retry=True,
        )

        self.app.chat.set("其他群")
        self.app.on_group_selected()
        self.assertTrue(self.app.retry_button.winfo_manager())

    def test_yanran_incremental_retry_uses_fixed_status_json_for_same_day(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "嫣然心水", "@yanran")
        self.app.reload_group_profiles("嫣然心水")
        self.app.day.set("2026-08-31")
        status_path = capture_status_path(self.app.status_root, "嫣然心水")
        save_capture_status(
            status_path,
            {
                "group_name": "嫣然心水",
                "date": "2026-08-31",
                "remarks": {"青苹果": {"status": "待复抓", "pending_message_ids": [42]}},
            },
        )
        output = Path(self.app_directory.name) / "result"
        group_output = output / "8.31-嫣然心水"
        group_output.mkdir(parents=True)
        self.app.output_path.set(str(output))

        with patch.object(self.app, "start_download") as start_download:
            self.app.retry_capture()
        start_download.assert_called_once_with(retry=True)

    def test_retry_requires_today_result_folder_and_refreshes_first_capture_marker(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "新澳六合彩资料", "@data")
        self.app.reload_group_profiles("新澳六合彩资料")
        self.app.day.set("2026-09-05")
        output = Path(self.app_directory.name) / "result"
        self.app.output_path.set(str(output))
        self.app.update_first_capture_status()
        self.assertEqual(self.app.first_capture_status.get(), "今日未首抓")

        dated = output / "9.5-新澳六合彩资料"
        dated.mkdir(parents=True)
        self.app.update_first_capture_status()
        self.assertEqual(self.app.first_capture_status.get(), "今日已首抓")
        dated.rmdir()
        self.app.update_first_capture_status()
        self.assertEqual(self.app.first_capture_status.get(), "今日未首抓")

        with patch("telegram_caption_downloader_gui.messagebox.showwarning") as warning, \
             patch.object(self.app, "start_download") as start_download:
            self.app.retry_capture()
        warning.assert_called_once_with("请先首抓", "请先首抓")
        start_download.assert_not_called()

    def test_other_group_incremental_retry_uses_its_own_status_json(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "新澳高手", "@expert")
        self.app.reload_group_profiles("新澳高手")
        self.app.day.set("2026-09-01")
        status_path = capture_status_path(self.app.status_root, "新澳高手")
        save_capture_status(
            status_path,
            {
                "group_name": "新澳高手",
                "date": "2026-09-01",
                "remarks": {"跑狗图": {"status": "待复抓", "pending_message_ids": []}},
            },
        )
        output = Path(self.app_directory.name) / "result"
        (output / "9.1-新澳高手").mkdir(parents=True)
        self.app.output_path.set(str(output))

        with patch.object(self.app, "start_download") as start_download:
            self.app.retry_capture()
        start_download.assert_called_once_with(retry=True)

    def test_settings_dialog_adds_renames_and_purges_profile_with_notes(self):
        dialog = GroupSettingsDialog(self.app)
        try:
            dialog.name.set("澳洲群")
            dialog.address.set("@australia")
            dialog.all_day.set(False)
            dialog.start_time.set("14:30")
            dialog.end_time.set("21:30")
            dialog.save_item()
            notes = self.app.program_root / "群配置" / "澳洲群.json"
            notes.write_text(json.dumps({"keywords": ["大小姐"]}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(self.app.chat.get(), "澳洲群")
            self.assertEqual(self.app.time_range.get(), "14:30 ～ 21:30")

            dialog.selected_name = "澳洲群"
            dialog.name.set("澳洲新群")
            dialog.address.set("https://t.me/australia")
            dialog.save_item()
            renamed_notes = self.app.program_root / "群配置" / "澳洲新群.json"
            self.assertEqual(load_notes(renamed_notes), {"大小姐": "大小姐"})

            with patch("telegram_caption_downloader_gui.messagebox.askyesno", return_value=True):
                dialog.delete_item()
            self.assertFalse(renamed_notes.exists())
            self.assertEqual(self.app.settings["groups"], [])
            with patch("telegram_caption_downloader_gui.os.startfile") as startfile:
                dialog.open_folder()
                startfile.assert_called_once()
        finally:
            dialog.window.destroy()

    def test_group_list_right_click_delete_purges_group_files(self):
        output = Path(self.app_directory.name) / "result"
        self.app.output_path.set(str(output))
        save_group_profile(self.app.program_root, self.app.settings, "", "右键群", "@right")
        save_group_profile(self.app.program_root, self.app.settings, "", "保留群", "@keep")
        self.app.reload_group_profiles("右键群")
        notes = self.app.program_root / "群配置" / "右键群.json"
        notes.write_text(json.dumps({"keywords": ["大小姐"]}, ensure_ascii=False), encoding="utf-8")
        folder = output / "10.18-右键群"
        folder.mkdir(parents=True)
        status_file = capture_status_path(self.app.status_root, "右键群")
        save_capture_status(status_file, {"group_name": "右键群", "date": "2026-10-18", "remarks": {}})
        log_dir = self.app.log_root / "右键群"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "2026-10-18.log").write_text("日志", encoding="utf-8")

        self.app.group_list.selection_set("右键群")
        with patch("telegram_caption_downloader_gui.messagebox.askyesno", return_value=False):
            self.app.delete_selected_group()
        self.assertTrue(notes.exists())
        self.assertTrue(folder.is_dir())

        with patch("telegram_caption_downloader_gui.messagebox.askyesno", return_value=True):
            self.app.delete_selected_group()
        self.assertFalse(notes.exists())
        self.assertFalse(folder.exists())
        self.assertFalse(status_file.exists())
        self.assertFalse(log_dir.exists())
        self.assertEqual([item["name"] for item in self.app.settings["groups"]], ["保留群"])
        self.assertFalse(self.app.group_list.exists("右键群"))
        self.assertEqual(self.app.chat.get(), "保留群")

    def test_group_delete_is_blocked_while_a_task_is_running(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "忙碌群", "@busy")
        self.app.reload_group_profiles("忙碌群")
        self.app.group_list.selection_set("忙碌群")
        self.app._busy = True
        try:
            with patch("telegram_caption_downloader_gui.messagebox.showinfo") as showinfo, \
                    patch("telegram_caption_downloader_gui.messagebox.askyesno") as askyesno:
                self.app.delete_selected_group()
            showinfo.assert_called_once()
            askyesno.assert_not_called()
            self.assertEqual([item["name"] for item in self.app.settings["groups"]], ["忙碌群"])
        finally:
            self.app._busy = False

    def test_open_notes_uses_selected_groups_same_named_file(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "备注群", "@notes")
        self.app.reload_group_profiles("备注群")
        with patch("telegram_caption_downloader_gui.os.startfile") as startfile:
            self.app.open_notes_file()
        startfile.assert_called_once_with(self.app.program_root / "群配置" / "备注群.json")

    def test_capture_clock_ticks_during_wait_and_preserves_final_status(self):
        with patch("telegram_caption_downloader_gui.time.monotonic", return_value=100) as clock:
            self.app._start_capture_progress()
            self.app.set_progress(330, 0, "扫描中：已发现 330 张")
            self.root.update()
            self.assertIn("已耗时 00:00", self.app.task_status.get())
            self.assertIn("计算中", self.app.task_status.get())
            clock.return_value = 160
            self.root.after(1100, self.root.quit)
            self.root.mainloop()
            self.assertIn("已耗时 01:00", self.app.task_status.get())
            self.assertIn("扫描中", self.app.task_status.get())
            clock.return_value = 170
            self.app.set_progress(1, 4, "预览下载：1 / 4", phase_started=160)
            self.root.update()
            self.assertEqual(self.app.task_status.get(),
                             "预览下载：1 / 4｜已耗时 01:10｜本阶段预计剩余 00:30")
            self.app.set_progress(2, 4, "预览下载：2 / 4", phase_started=160)
            self.app.set_busy(False, "完成")
            self.root.update()
            self.assertEqual(self.app.task_status.get(), "完成")
            self.assertIsNone(self.app._capture_timer)

    def test_capture_failure_stops_clock_and_next_run_ignores_old_updates(self):
        self.app._start_capture_progress()
        self.app.set_progress(1, 10, "旧任务进度")
        self.app.set_busy(False, "操作失败")
        self.root.update()
        self.assertIn("操作失败", self.app.task_status.get())
        self.assertIn("已耗时", self.app.task_status.get())
        self.assertIsNone(self.app._capture_timer)
        self.app._start_capture_progress()
        self.app.set_progress(1, 10, "过期进度")
        self.app._start_capture_progress()
        self.root.update()
        self.assertNotIn("过期", self.app.task_status.get())
        self.app._stop_capture_progress()

    def test_worker_failure_freezes_capture_time_before_error_dialog(self):
        observed = []

        def operation():
            self.app.set_progress(0, 10, "预览下载：0 / 10")
            raise RuntimeError("test connection failure")

        def show_error(*_args):
            observed.append((self.app.task_status.get(), self.app._capture_timer))
            self.root.after_idle(self.root.quit)

        self.app._start_capture_progress()
        timeout = self.root.after(3000, self.root.quit)
        try:
            with patch("telegram_caption_downloader_gui.messagebox.showerror", side_effect=show_error):
                self.app.run_worker(operation, lambda _result: self.fail("unexpected success"))
                self.root.mainloop()
            self.assertEqual(len(observed), 1)
            self.assertIn("操作失败｜已耗时", observed[0][0])
            self.assertIsNone(observed[0][1])
            self.root.update()
            self.assertEqual(self.app.task_status.get(), observed[0][0])
        finally:
            self.root.after_cancel(timeout)

    def test_status_log_open_folder_and_worker_paths(self):
        self.root.update()
        with tempfile.TemporaryDirectory() as directory, patch(
            "telegram_caption_downloader_gui.os.startfile"
        ) as startfile:
            output = Path(directory) / "new-result"
            save_group_profile(self.app.program_root, self.app.settings, "", "打开群", "@open")
            self.app.reload_group_profiles("打开群")
            self.app.day.set("2026-09-02")
            dated = output / "9.2-打开群"
            dated.mkdir(parents=True)
            self.app.output_path.set(str(output))
            self.app.open_output()
            startfile.assert_called_once_with(dated)

        self.app.log("可见日志")
        self.root.update()
        self.assertIn("可见日志", self.app.log_box.get("1.0", "end"))
        self.app.set_busy(True, "处理中")
        self.assertEqual(self.app.task_status.get(), "处理中")
        self.app.set_busy(False, "完成")
        self.app.set_progress(3, 10, "正在处理：3 / 10")
        self.root.update()
        self.assertFalse(hasattr(self.app, "progress"))
        self.assertEqual(self.app.task_status.get(), "正在处理：3 / 10")

        original_after = self.app.root.after
        self.app.root.after = lambda _delay, callback: callback()

        class ImmediateThread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

        successes = []
        with patch("telegram_caption_downloader_gui.threading.Thread", ImmediateThread):
            self.app.run_worker(lambda: 42, successes.append)
            with patch("telegram_caption_downloader_gui.messagebox.showerror") as showerror:
                self.app.run_worker(lambda: (_ for _ in ()).throw(RuntimeError("测试失败")), successes.append)
                showerror.assert_called_once()
        self.app.root.after = original_after
        self.assertEqual(successes, [42])

    def test_clear_output_clears_every_group_and_date_after_confirmation(self):
        output = Path(self.app_directory.name) / "result"
        nested = output / "测试群" / "大小姐"
        nested.mkdir(parents=True)
        (nested / "image.jpg").write_bytes(b"image")
        other_group = output / "另一个群" / "其他备注"
        other_group.mkdir(parents=True)
        (other_group / "other.jpg").write_bytes(b"image")
        (output / "提取报告.csv").write_text("report", encoding="utf-8")
        self.app.output_path.set(str(output))

        with patch("telegram_caption_downloader_gui.messagebox.askyesno", return_value=False) as askyesno:
            self.app.clear_output()

        askyesno.assert_called_once()
        self.assertTrue((nested / "image.jpg").is_file())
        self.assertTrue((other_group / "other.jpg").is_file())
        self.assertTrue((output / "提取报告.csv").is_file())

        with patch("telegram_caption_downloader_gui.messagebox.askyesno", return_value=True) as askyesno:
            self.app.clear_output()

        askyesno.assert_called_once()
        self.assertTrue(output.is_dir())
        self.assertEqual(list(output.iterdir()), [])
        self.assertEqual(self.app.task_status.get(), "已清除结果：3 项")

    def test_clear_output_ignores_selected_group_and_day(self):
        # 用户要求：不分日期、不分当前选中群，点一次就全清。
        output = Path(self.app_directory.name) / "result-all"
        old_day = output / "9.1-测试群" / "2026-09-01"
        old_day.mkdir(parents=True)
        (old_day / "old.jpg").write_bytes(b"x")
        today_dir = output / "9.1-测试群" / "2026-10-18"
        today_dir.mkdir(parents=True)
        (today_dir / "today.jpg").write_bytes(b"x")
        save_group_profile(self.app.program_root, self.app.settings, "", "测试群", "@a")
        self.app.reload_group_profiles("测试群")
        self.app.output_path.set(str(output))
        self.app.day.set("2026/10/05")
        with patch("telegram_caption_downloader_gui.messagebox.askyesno", return_value=True):
            self.app.clear_output()
        self.assertEqual(list(output.iterdir()), [])
        self.assertEqual(self.app.task_status.get(), "已清除结果：1 项")

    def test_auto_close_info_dialog_closes_itself_and_can_be_closed_early(self):
        root = Tk()
        root.geometry("300x200+80+80")
        root.deiconify()
        root.update()
        try:
            gui.show_auto_close_info(root, "完成", "内容", seconds=0.2)
            root.update()
            windows = [item for item in root.winfo_children() if isinstance(item, Toplevel)]
            self.assertEqual(len(windows), 1)
            window = windows[0]
            # 手动点「确定」要能立刻关掉，不用等 5 秒
            def find_button(widget):
                for child in widget.winfo_children():
                    if hasattr(child, "invoke") and str(child.cget("text")) == "确定":
                        return child
                    found = find_button(child)
                    if found is not None:
                        return found
                return None

            ok_button = find_button(window)
            self.assertIsNotNone(ok_button)
            ok_button.invoke()
            root.update()
            self.assertEqual([item for item in root.winfo_children() if isinstance(item, Toplevel)], [])

            # 不手动关闭时也会自己消失
            gui.show_auto_close_info(root, "完成", "内容", seconds=0.2)
            root.update()
            self.assertEqual(len([i for i in root.winfo_children() if isinstance(i, Toplevel)]), 1)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and any(
                isinstance(item, Toplevel) for item in root.winfo_children()
            ):
                root.update()
                time.sleep(0.02)
            self.assertEqual([item for item in root.winfo_children() if isinstance(item, Toplevel)], [])
        finally:
            root.destroy()

    def test_auto_close_info_does_nothing_when_parent_hidden(self):
        # 主窗口不可见时不弹窗，避免测试里堆窗口。
        self.root.withdraw()
        self.root.update()
        gui.show_auto_close_info(self.root, "完成", "内容", seconds=0.1)
        self.assertEqual([item for item in self.root.winfo_children() if isinstance(item, Toplevel)], [])

    def test_secret_prompt_and_invalid_actions_are_handled(self):
        original_after = self.app.root.after
        self.app.root.after = lambda _delay, callback: callback()
        with patch("telegram_caption_downloader_gui.simpledialog.askstring", return_value=" 123456 "):
            self.assertEqual(self.app.ask_secret("验证码", "请输入"), "123456")
        self.app.root.after = original_after

        self.app.api_id.set("错误")
        with patch("telegram_caption_downloader_gui.messagebox.showwarning") as warning:
            self.app.login()
            self.app.start_download()
            self.assertEqual(warning.call_count, 2)

    def test_login_uses_existing_local_session(self):
        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(first_name="测试账号", username=None, id=7, phone="8613800000000")

            def disconnect(self):
                pass

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        with (
            patch("telethon.sync.TelegramClient", FakeClient),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
        ):
            self.app.login()
        self.assertTrue(self.app.logged_in)
        self.assertEqual(self.app.account_status.get(), "已登录：测试账号")
        self.assertEqual(
            load_saved_credentials(account_directory(Path(self.app_directory.name), self.account_id) / "credentials.bin"),
            (12345, "secret-hash", "+8613800000000"),
        )

    def test_startup_opens_account_chooser_without_selecting_or_connecting(self):
        # 全新数据目录 + 旧版根目录凭据：启动时应先迁移，再打开账号弹窗且不自动登录。
        app_data = Path(self.app_directory.name) / "legacy-app"
        app_data.mkdir()
        save_saved_credentials(app_data / "credentials.bin", 9876, "saved-hash", "+8613900000000")
        (app_data / "accounts.json").write_text(
            json.dumps({"accounts": [{"id": "legacy", "name": "原有账号", "user_id": None}]}), encoding="utf-8")
        second_root = Tk()
        try:
            with patch("telethon.sync.TelegramClient") as client:
                second_app = TelegramDownloaderApp(
                    second_root,
                    app_data=app_data,
                    program_root=Path(self.app_directory.name) / "second-program",
                )
                second_root.update()
                client.assert_not_called()
            self.assertFalse(second_app.logged_in)
            self.assertEqual(second_app.selected_account_id, "")
            self.assertTrue(second_app.login_settings_window.winfo_exists())
            dialog = second_app.login_settings_window
            self.assertAlmostEqual(dialog.winfo_rootx() + dialog.winfo_width() / 2,
                                   second_root.winfo_rootx() + second_root.winfo_width() / 2, delta=3)
            self.assertAlmostEqual(dialog.winfo_rooty() + dialog.winfo_height() / 2,
                                   second_root.winfo_rooty() + second_root.winfo_height() / 2, delta=3)
            self.assertGreaterEqual(dialog.winfo_rootx(), 0)
            self.assertGreaterEqual(dialog.winfo_rooty(), 0)
            self.assertIsNone(second_root.grab_current(), "账号弹窗不应锁住主窗口的关闭操作")
            self.assertEqual(second_app.api_id.get(), "")
            second_app.select_account(second_app.account_profiles[0]["id"])
            self.assertEqual(second_app.api_id.get(), "9876")
            self.assertEqual(second_app.api_hash.get(), "saved-hash")
            self.assertEqual(second_app.phone.get(), "+8613900000000")
        finally:
            second_root.destroy()

    def test_restart_uses_last_manually_selected_account_without_chooser(self):
        from unittest.mock import MagicMock
        from telethon.sessions import StringSession
        from telegram_caption_downloader_gui import load_selected_account
        app_data = Path(self.app_directory.name)
        second = create_account_profile(app_data, "备用号", 678, "other-secret", "+8613900000000")
        self.app.refresh_account_profiles()
        for account_id, user_id, phone in [(self.account_id, 7, "8613800000000"),
                                           (second["id"], 8, "8613900000000")]:
            self.app.select_account(account_id)
            session = account_directory(app_data, account_id) / "session.bin"
            save_session_string(session, fake_session_string())
            original_session = session.read_bytes()
            for _ in range(2):
                root = Tk()
                client = MagicMock()
                client.is_user_authorized.return_value = True
                client.get_me.return_value = SimpleNamespace(id=user_id, phone=phone, first_name="测试", username=None)
                try:
                    with (
                        patch("telethon.sync.TelegramClient", return_value=client) as factory,
                        patch.object(TelegramDownloaderApp, "run_worker",
                                     lambda _, operation, success, on_failure=None: success(operation())),
                        patch("telegram_caption_downloader_gui.messagebox.showinfo") as info,
                    ):
                        app = TelegramDownloaderApp(root, app_data=app_data, program_root=self.app.program_root,
                                                    status_root=self.app.status_root)
                        root.update()
                        self.assertTrue(app.logged_in)
                        self.assertEqual(app.active_account_id, account_id)
                        self.assertFalse(hasattr(app, "login_settings_window"))
                        self.assertIsInstance(factory.call_args.args[0], StringSession)
                        info.assert_not_called()
                        client.send_code_request.assert_not_called()
                    self.assertEqual(load_selected_account(app_data), account_id)
                    self.assertEqual(session.read_bytes(), original_session)
                    self.assertEqual(app.status_root, self.app.status_root)
                    self.assertEqual(app.settings, self.app.settings)
                finally:
                    root.destroy()

    def test_automatic_login_expired_session_never_sends_code_or_switches_account(self):
        from unittest.mock import MagicMock
        from telegram_caption_downloader_gui import load_selected_account
        root = Path(self.app_directory.name)
        session = account_directory(root, self.account_id) / "session.bin"
        save_session_string(session, fake_session_string())
        original_session = session.read_bytes()
        client = MagicMock()
        client.is_user_authorized.return_value = False
        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        with patch("telethon.sync.TelegramClient", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "失效"):
                self.app.login(automatic=True)
        client.send_code_request.assert_not_called()
        client.sign_in.assert_not_called()
        self.assertFalse(self.app.logged_in)
        self.assertEqual(self.app.selected_account_id, self.account_id)
        self.assertEqual(load_selected_account(root), self.account_id)
        self.assertEqual(session.read_bytes(), original_session)

    def test_invalid_saved_choice_prompts_without_fallback_or_overwrite(self):
        self.app.select_account("")
        preference = Path(self.app_directory.name) / "account_selection.json"
        invalid = '{"account_id":"../bad"}'
        preference.write_text(invalid, encoding="utf-8")
        with patch("telethon.sync.TelegramClient") as client, patch("telegram_caption_downloader_gui.messagebox.showwarning"):
            self.app._restore_selected_account()
            self.assertEqual(self.app.selected_account_id, "")
            self.assertTrue(self.app.login_settings_window.winfo_exists())
            client.assert_not_called()
        self.assertEqual(preference.read_text(encoding="utf-8"), invalid)

    def test_missing_local_session_or_credentials_never_uses_another_account(self):
        from telegram_caption_downloader_gui import load_selected_account
        app_data = Path(self.app_directory.name)
        with patch("telethon.sync.TelegramClient") as client, patch("telegram_caption_downloader_gui.messagebox.showwarning") as warning:
            self.app.login(automatic=True)
            client.assert_not_called()
            warning.assert_called_once()
            self.assertFalse(self.app.logged_in)
            self.assertEqual(self.app.selected_account_id, self.account_id)
        self.app.select_account("")
        credentials = account_directory(app_data, self.account_id) / "credentials.bin"
        before = credentials.read_bytes()
        with (patch("telegram_caption_downloader_gui.load_saved_credentials", return_value=None),
              patch("telegram_caption_downloader_gui.messagebox.showwarning"),
              patch("telethon.sync.TelegramClient") as client):
            self.app._restore_selected_account()
            client.assert_not_called()
            self.assertTrue(self.app.login_settings_window.winfo_exists())
            self.assertEqual(self.app.selected_account_id, "")
        self.assertEqual(load_selected_account(app_data), self.account_id)
        self.assertEqual(credentials.read_bytes(), before)

    def test_new_install_dialog_allows_typing_and_closing_without_login(self):
        root = Tk()
        try:
            with patch("telethon.sync.TelegramClient") as client:
                app = TelegramDownloaderApp(root, app_data=Path(self.app_directory.name) / "empty-login",
                                            program_root=Path(self.app_directory.name) / "empty-program")
                root.update()
                self.assertTrue(app.adding_account)
                for entry in app.account_fields:
                    self.assertEqual(str(entry.cget("state")), "normal")
                app.phone_entry.insert(0, "+8613900000000")
                self.assertEqual(app.phone.get(), "+8613900000000")
                self.assertIsNone(root.grab_current())
                app.login_close_button.invoke()
                root.update()
                self.assertFalse(app.login_settings_window.winfo_exists())
                client.assert_not_called()
        finally:
            root.destroy()

    def test_switch_account_keeps_everything_else_shared_and_requires_login(self):
        new = create_account_profile(Path(self.app_directory.name), "第二个号", 678, "other-hash", "+8613900000000")
        original = (self.app.settings, self.app.status_root, self.app.log_root, self.app.output_path.get(), self.app.chat.get())
        self.app.logged_in = True
        self.app.active_account_id = self.account_id
        self.app.refresh_account_profiles()
        self.app.select_account(new["id"])
        self.assertFalse(self.app.logged_in)
        self.assertEqual(self.app.active_account_id, "")
        self.assertEqual(self.app.selected_credentials(), (678, "other-hash", "+8613900000000"))
        self.assertEqual(original, (self.app.settings, self.app.status_root, self.app.log_root, self.app.output_path.get(), self.app.chat.get()))
        with patch("telegram_caption_downloader_gui.messagebox.showwarning"), patch.object(self.app, "run_worker") as worker:
            self.app.start_download()
            worker.assert_not_called()

    def test_busy_blocks_account_switch_add_and_duplicate_login(self):
        self.app.open_login_settings()
        self.app.set_busy(True)
        self.app.begin_add_account()
        self.app.select_account("")
        with patch.object(self.app, "run_worker") as worker:
            self.app.login()
            worker.assert_not_called()
        self.assertEqual(self.app.selected_account_id, self.account_id)
        self.assertEqual(str(self.app.account_combo.cget("state")), "disabled")
        self.app.set_busy(False)
        self.assertEqual(str(self.app.account_combo.cget("state")), "readonly")

    def test_failed_account_login_cannot_leave_previous_account_logged_in(self):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.is_user_authorized.return_value = True
        client.get_me.return_value = SimpleNamespace(first_name="wrong", id=8, phone="8613900000000")
        self.app.logged_in = True
        self.app.active_account_id = self.account_id
        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        with patch("telethon.sync.TelegramClient", return_value=client):
            with self.assertRaises(RuntimeError):
                self.app.login()
        self.assertFalse(self.app.logged_in)
        self.assertEqual(self.app.active_account_id, "")

    def test_add_account_code_and_password_login_preserves_old_account(self):
        from unittest.mock import MagicMock
        from telegram_caption_downloader_gui import load_selected_account
        from telethon.errors import SessionPasswordNeededError
        from telethon.sessions import StringSession
        root = Path(self.app_directory.name)
        folder = account_directory(root, self.account_id)
        old_credentials = (folder / "credentials.bin").read_bytes()
        (folder / "account.session").write_bytes(b"old session, must not move")
        self.app.open_login_settings()
        self.app.begin_add_account()
        self.assertEqual(self.app.api_hash.get(), "")
        self.app.account_name.set("备用号")
        self.app.api_id.set("678")
        self.app.api_hash.set("new-secret")
        self.app.phone.set("+8613900000000")
        client = MagicMock()
        client.is_user_authorized.return_value = False
        client.sign_in.side_effect = [SessionPasswordNeededError(None), None]
        client.get_me.return_value = SimpleNamespace(id=8, phone="8613900000000", first_name="新号", username=None)
        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        with (
            patch("telethon.sync.TelegramClient", return_value=client) as factory,
            patch.object(self.app, "ask_secret", side_effect=["123456", "secret-password"]) as ask,
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
        ):
            self.app.login()
        new_id = self.app.active_account_id
        self.assertEqual(load_selected_account(root), new_id)
        self.assertTrue(self.app.logged_in)
        self.assertNotEqual(new_id, self.account_id)
        self.assertIsInstance(factory.call_args.args[0], StringSession)
        self.assertEqual(ask.call_count, 2)
        client.send_code_request.assert_called_once_with("+8613900000000")
        client.sign_in.assert_any_call(password="secret-password")
        self.assertEqual((folder / "credentials.bin").read_bytes(), old_credentials)
        self.assertEqual((folder / "account.session").read_bytes(), b"old session, must not move")
        self.assertEqual(len(load_account_profiles(root)), 2)
        all_logs = "\n".join(path.read_text(encoding="utf-8") for path in self.app.log_root.rglob("*.log"))
        for secret in ("123456", "secret-password", "new-secret", "13900000000"):
            self.assertNotIn(secret, all_logs)

    def test_cancel_or_sensitive_login_error_does_not_log_secret_or_authenticate(self):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.is_user_authorized.return_value = False
        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        with (
            patch("telethon.sync.TelegramClient", return_value=client),
            patch.object(self.app, "ask_secret", side_effect=RuntimeError("登录已取消")),
        ):
            with self.assertRaisesRegex(RuntimeError, "登录已取消"):
                self.app.login()
        self.assertFalse(self.app.logged_in)
        client.sign_in.assert_not_called()
        client.connect.side_effect = RuntimeError("secret-hash +8613800000000 123456")
        with patch("telethon.sync.TelegramClient", return_value=client):
            with self.assertRaises(RuntimeError) as result:
                self.app.login()
        self.assertNotIn("secret-hash", str(result.exception))
        self.assertEqual(self.app.active_account_id, "")

    def test_saved_account_login_ignores_phone_tampering(self):
        self.app.phone.set("+8613900000000")
        with patch("telethon.sync.TelegramClient") as factory, patch("telegram_caption_downloader_gui.messagebox.showwarning"):
            self.app.login()
        factory.assert_not_called()
        self.assertFalse(self.app.logged_in)

    def test_different_account_download_reuses_same_group_output_and_retry_status(self):
        root = Path(self.app_directory.name)
        second = create_account_profile(root, "备用号", 678, "other-secret", "+8613900000000")
        save_group_profile(self.app.program_root, self.app.settings, "", "共享群", "@shared")
        self.app.reload_group_profiles("共享群")
        (self.app.program_root / "群配置" / "共享群.json").write_text('{"keywords":["资料"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")
        sessions = []
        first_session, second_session = fake_session_string(1), fake_session_string(2)
        save_session_string(account_directory(root, self.account_id) / "session.bin", first_session)
        save_session_string(account_directory(root, second["id"]) / "session.bin", second_session)

        class Message:
            id, raw_text, grouped_id = 1, "资料", None
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True

            async def download_media(self, file):
                Path(file).write_bytes(b"shared image")
                return file

        class Client:
            identity = SimpleNamespace(id=7, phone="8613800000000")

            def __init__(self, session, *_args, **_kwargs):
                sessions.append(session)

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return self.identity

            def get_entity(self, address):
                return address

            def iter_messages(self, _entity, **_kwargs):
                return iter([Message()])

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with patch("telethon.sync.TelegramClient", Client), patch("telegram_caption_downloader_gui.messagebox.showinfo"):
            self.app.start_download()
            status_path = capture_status_path(self.app.status_root, "共享群")
            original_status = load_capture_status(status_path)
            self.app.refresh_account_profiles()
            self.app.select_account(second["id"])
            self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, second["id"], 8
            Client.identity = SimpleNamespace(id=8, phone="8613900000000")
            self.app.start_download(retry=True)
        self.assertEqual([session.save() for session in sessions], [first_session, second_session])
        self.assertEqual(load_capture_status(status_path)["first_capture_time"], original_status["first_capture_time"])
        self.assertEqual(load_capture_status(status_path)["remarks"], original_status["remarks"])
        self.assertEqual(len(list((root / "results").rglob("*.jpg"))), 1)
        self.assertEqual(len(list(self.app.status_root.glob("*.json"))), 1)
        self.assertTrue((self.app.log_root / "共享群").is_dir())
        self.assertFalse((self.app.log_root / second["id"]).exists())

    def test_private_group_uses_bound_account_session_without_switching_current_login(self):
        from telethon.tl.types import InputPeerChannel
        root = Path(self.app_directory.name)
        second = create_account_profile(root, "备用号", 678, "other-secret", "+8613900000000")
        second_session = fake_session_string(3)
        save_saved_credentials(account_directory(root, second["id"]) / "credentials.bin",
                               678, "other-secret", "+8613900000000")
        save_session_string(account_directory(root, second["id"]) / "session.bin", second_session)
        save_group_profile(self.app.program_root, self.app.settings, "", "慕熙会员群", "",
                           chat_id=-1004401898428, bound_account_id=second["id"])
        self.app.refresh_account_profiles()
        self.app.reload_group_profiles("慕熙会员群")
        self.app.day.set("2026-09-02")
        self.app.output_path.set(str(root / "results"))
        sessions = []

        class Client:
            def __init__(self, session, *_args, **_kwargs):
                sessions.append(session)

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=8, phone="8613900000000")

            def get_input_entity(self, _value):
                return InputPeerChannel(channel_id=4401898428, access_hash=7)

            def iter_messages(self, _entity, **_kwargs):
                return iter(())

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with patch("telethon.sync.TelegramClient", Client), patch("telegram_caption_downloader_gui.messagebox.showinfo"):
            self.app.start_download()
        self.assertEqual([session.save() for session in sessions], [second_session])
        self.assertTrue(self.app.logged_in)
        self.assertEqual(self.app.active_account_id, self.account_id)
        logs = "\n".join(path.read_text(encoding="utf-8") for path in self.app.log_root.rglob("*.log"))
        self.assertIn("本次私密群抓取使用", logs)
        self.assertIn("-1004401898428", logs)

    def test_private_group_missing_bound_session_stops_without_switching(self):
        root = Path(self.app_directory.name)
        second = create_account_profile(root, "备用号", 678, "other-secret", "+8613900000000")
        save_group_profile(self.app.program_root, self.app.settings, "", "慕熙会员群", "",
                           chat_id=-1004401898428, bound_account_id=second["id"])
        self.app.refresh_account_profiles()
        self.app.reload_group_profiles("慕熙会员群")
        self.app.day.set("2026-09-02")
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with patch("telethon.sync.TelegramClient") as factory, \
             patch("telegram_caption_downloader_gui.messagebox.showwarning") as warning:
            self.app.start_download()
        factory.assert_not_called()
        warning.assert_called_once()
        self.assertTrue(self.app.logged_in)
        self.assertEqual(self.app.active_account_id, self.account_id)

    def test_private_group_expired_bound_session_does_not_clear_current_login(self):
        root = Path(self.app_directory.name)
        second = create_account_profile(root, "备用号", 678, "other-secret", "+8613900000000")
        save_saved_credentials(account_directory(root, second["id"]) / "credentials.bin",
                               678, "other-secret", "+8613900000000")
        save_session_string(account_directory(root, second["id"]) / "session.bin", fake_session_string(4))
        save_group_profile(self.app.program_root, self.app.settings, "", "慕熙会员群", "",
                           chat_id=-1004401898428, bound_account_id=second["id"])
        self.app.refresh_account_profiles()
        self.app.reload_group_profiles("慕熙会员群")
        self.app.day.set("2026-09-02")

        class ExpiredClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return False

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with patch("telethon.sync.TelegramClient", ExpiredClient), \
             patch("telegram_caption_downloader_gui.messagebox.showinfo"):
            with self.assertRaisesRegex(RuntimeError, "登录已失效"):
                self.app.start_download()
        self.assertTrue(self.app.logged_in)
        self.assertEqual(self.app.active_account_id, self.account_id)

    def test_public_group_still_requires_current_account_login(self):
        save_group_profile(self.app.program_root, self.app.settings, "", "公开测试群", "@public")
        self.app.reload_group_profiles("公开测试群")
        self.app.logged_in, self.app.active_account_id = False, ""
        with patch("telethon.sync.TelegramClient") as factory, \
             patch("telegram_caption_downloader_gui.messagebox.showwarning") as warning:
            self.app.start_download()
        factory.assert_not_called()
        warning.assert_called_once()

    def test_group_settings_dialog_saves_and_reloads_private_group(self):
        root = Path(self.app_directory.name)
        second = create_account_profile(root, "备用号", 678, "other-secret", "+8613900000000")
        self.app.refresh_account_profiles()
        dialog = GroupSettingsDialog(self.app)
        try:
            dialog.new_item()
            dialog.name.set("慕熙会员群")
            dialog.chat_id.set("-1004401898428")
            dialog.bound_account_combo.current(1)
            dialog.bound_account.set(dialog.bound_account_combo.get())
            dialog.save_item()
            profile = next(item for item in self.app.settings["groups"] if item["name"] == "慕熙会员群")
            self.assertEqual(profile["chat_id"], -1004401898428)
            self.assertEqual(profile["bound_account_id"], second["id"])
            self.assertEqual(profile["address"], "")

            dialog.tree.selection_set(dialog.tree.get_children()[-1])
            dialog.select_item()
            self.assertEqual(dialog.chat_id.get(), "-1004401898428")
            self.assertEqual(dialog.selected_bound_account_id(), second["id"])
            self.assertEqual(str(dialog.address_entry.cget("state")), "disabled")
        finally:
            dialog.window.destroy()

    def test_retry_incremental_new_source_stops_at_day_start(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "测试群", "@old | @new")
        self.app.reload_group_profiles("测试群")
        notes_file = self.app.program_root / "群配置" / "测试群.json"
        notes_file.write_text('{"keywords":["九肖统计"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")
        (root / "results" / "9.2-测试群").mkdir(parents=True)
        status_path = capture_status_path(self.app.status_root, "测试群")
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps({
            "group_name": "测试群",
            "date": "2026-09-02",
            "remarks": {"九肖统计": {"status": "待处理",
                                     "pending_messages": [{"source": "@old", "id": 3}]}},
            "last_message_ids": {"@old": 5},
        }, ensure_ascii=False), encoding="utf-8")

        consumed = []
        in_day = SimpleNamespace(id=10, raw_text="九肖统计", grouped_id=None,
                                 date=datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc),
                                 file=None, photo=False)
        older = SimpleNamespace(id=1, raw_text="", grouped_id=None,
                                date=datetime(2026, 8, 1, 4, 0, tzinfo=timezone.utc),
                                file=None, photo=False)

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def get_messages(self, *_args, **_kwargs):
                return []

            def iter_messages(self, _entity, **_kwargs):
                def stream():
                    consumed.append(10)
                    yield in_day
                    consumed.append(1)
                    yield older
                    raise AssertionError("增量扫描未在当天开始处停止")

                return stream()

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with patch("telethon.sync.TelegramClient", Client), patch("telegram_caption_downloader_gui.messagebox.showinfo"):
            self.app.start_download(retry=True)
        self.assertIn(1, consumed)
        logs = "\n".join(path.read_text(encoding="utf-8") for path in self.app.log_root.rglob("*.log"))
        self.assertIn("读取消息ID > 0", logs)

    def test_retry_rescans_old_messages_after_keyword_alias_is_added(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "嫣然心水", "@yanran")
        self.app.reload_group_profiles("嫣然心水")
        notes_file = self.app.program_root / "群配置" / "嫣然心水.json"
        notes_file.write_text('{"keywords":["阿尔法天狼星"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")

        class Message:
            id, raw_text, grouped_id = 1, "阿尔法 天狼星", None
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True

            async def download_media(self, file):
                Path(file).write_bytes(b"alpha image")
                return file

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, _entity, **_kwargs):
                return iter([Message()])

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with patch("telethon.sync.TelegramClient", Client), patch("telegram_caption_downloader_gui.messagebox.showinfo"):
            self.app.start_download()
            self.assertFalse((root / "results" / "9.2-嫣然心水" / "阿尔法天狼星").exists())
            notes_file.write_text(
                '{"keywords":["阿尔法天狼星/阿尔法 天狼星"]}', encoding="utf-8"
            )
            self.app.start_download(retry=True)

        files = list((root / "results" / "9.2-嫣然心水" / "阿尔法天狼星").glob("*.jpg"))
        self.assertEqual(len(files), 1)

    def test_exact_caption_status_and_legacy_pending_revalidation(self):
        root = Path(self.app_directory.name)
        progress_updates = []
        original_progress = self.app.set_progress

        def record_progress(current, total, status, **kwargs):
            progress_updates.append((current, total, status))
            original_progress(current, total, status, **kwargs)

        self.app.set_progress = record_progress
        save_group_profile(self.app.program_root, self.app.settings, "", "黄大仙新澳", "@huang")
        self.app.reload_group_profiles("黄大仙新澳")
        (self.app.program_root / "群配置" / "黄大仙新澳.json").write_text(
            '{"keywords":["战狼"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")

        class Message:
            grouped_id = None
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True

            def __init__(self, message_id, caption):
                self.id, self.raw_text = message_id, caption

            async def download_media(self, file):
                if file is bytes:
                    return b"preview"
                Path(file).write_bytes(b"image")
                return file

        messages = [Message(1, "🌟战狼"), Message(2, "🌟战狼、赌神"), Message(3, "")]

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, _entity, **kwargs):
                return iter([] if "min_id" in kwargs else messages[:2])

            def get_messages(self, _entity, ids):
                return [next((message for message in messages if message.id == item), None) for item in ids]

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        status_path = capture_status_path(self.app.status_root, "黄大仙新澳")
        group_output = root / "results" / "9.2-黄大仙新澳"
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
            patch("telegram_caption_downloader_gui.get_ocr_engine", return_value=object()),
            patch("telegram_caption_downloader_gui.ocr_labels_from_payload", return_value=set()),
            patch("telegram_caption_downloader_gui.image_groups_are_similar", return_value=False),
        ):
            self.app.start_download()
            self.root.update()
            for phase in ("备注匹配", "预览下载", "图片比对", "加载 GPU OCR", "文字识别", "正在处理", "保存结果"):
                self.assertTrue(any(status.startswith(phase) for _, _, status in progress_updates), phase)
            for phase in ("预览下载", "文字识别"):
                self.assertTrue(any(current == total and total > 0 and status.startswith(phase)
                                    for current, total, status in progress_updates), phase)
            self.assertTrue(self.app.task_status.get().startswith("完成："))
            status = load_capture_status(status_path)
            self.assertEqual(status["remarks"]["战狼"]["status"], "已完成")
            self.assertEqual(status["remarks"]["战狼"]["pending_messages"], [])
            self.assertEqual((group_output / "未匹配备注.txt").read_text(encoding="utf-8-sig"), "")

            for pending, remaining in [([2], []), ([2, 3, 4], [3, 4])]:
                with self.subTest(pending=pending):
                    status["remarks"]["战狼"].update(status="部分完成", pending_message_ids=pending)
                    save_capture_status(status_path, status)
                    self.app.start_download(retry=True)
                    actual = load_capture_status(status_path)["remarks"]["战狼"]
                    self.assertEqual(actual["pending_messages"],
                                     [{"source": "@huang", "id": item} for item in remaining])
                    self.assertEqual(actual["image_count"], 1)
                    self.assertEqual(actual["status"], "部分完成" if remaining else "已完成")
                    self.assertEqual((group_output / "未匹配备注.txt").read_text(encoding="utf-8-sig"),
                                     "战狼" if remaining else "")
            self.assertEqual(len(list(group_output.rglob("*.jpg"))), 1)
            from unittest.mock import AsyncMock
            with patch.object(Message, "download_media", new=AsyncMock(side_effect=OSError("test download failure"))):
                with self.assertRaises(OcrError):
                    self.app.start_download()
            failed = load_capture_status(status_path)["remarks"]["战狼"]
            self.assertEqual(failed["pending_messages"],
                             [{"source": "@huang", "id": 3}, {"source": "@huang", "id": 4}])
            self.assertEqual(failed["image_count"], 1)
            self.assertEqual(failed["status"], "部分完成")
            preview_updates = [(current, total) for current, total, status in progress_updates
                               if status.startswith("预览下载")]
            self.assertGreater(preview_updates[-1][1], 0)
            self.assertEqual(*preview_updates[-1])
            self.root.update()
            self.assertFalse(self.app.task_status.get().startswith("完成："))

    def test_yanran_tianji_flow_extends_by_ocr_or_any_image_pair_until_both_miss(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "嫣然心水", "@yanran")
        self.app.reload_group_profiles("嫣然心水")
        (self.app.program_root / "群配置" / "嫣然心水.json").write_text(
            '{"keywords":["天机阁杀料"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")
        preview_calls = []
        ocr_calls = []

        class Message:
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True

            def __init__(self, message_id, caption, group_id, preview_payload):
                self.id = message_id
                self.raw_text = caption
                self.grouped_id = group_id
                self.preview_payload = preview_payload

            async def download_media(self, file):
                if file is bytes:
                    preview_calls.append(self.id)
                    return self.preview_payload
                Path(file).write_bytes(b"image")
                return file

        payloads = {}
        for message_id in range(1, 21):
            color = (40, 90, 220)
            if message_id in {3, 4, 17, 18}:
                color = (0, 0, 0)
            elif message_id in {5, 11, 13}:
                color = (210, 30, 70)
            elif message_id in {15, 16}:
                color = (255, 255, 255)
            image = Image.new("RGB", (72, 128), color)
            image.putpixel((0, 0), (message_id, 1, 2))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            payloads[message_id] = buffer.getvalue()
        messages = []
        for first_id in range(1, 20, 2):
            # Non-first blue images rescue groups whose red first image misses OCR.
            messages.extend([
                Message(first_id + 1, "天机阁杀料" if first_id == 9 else "", first_id, payloads[first_id + 1]),
                Message(first_id, "", first_id, payloads[first_id]),
            ])
        outside_range = Message(21, "", 21, b"21")
        outside_range.date = datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc)
        messages.insert(0, outside_range)

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, _entity, **_kwargs):
                return iter(messages)

        def fake_ocr(payload, labels, _engine, on_error=None):
            ocr_calls.append(payload)
            return {"天机阁杀料"} if payload in {payloads[7], payloads[15]} else set()

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
            patch("telegram_caption_downloader_gui.get_ocr_engine", return_value=object()),
            patch("telegram_caption_downloader_gui.ocr_labels_from_payload", side_effect=fake_ocr),
            patch("telegram_caption_downloader_gui.image_groups_are_similar",
                  wraps=image_groups_are_similar) as similarity,
        ):
            self.app.start_download()
        self.assertCountEqual(preview_calls, range(3, 19))
        self.assertCountEqual(ocr_calls, [payloads[message_id] for message_id in (3, 5, 7, 11, 13, 15, 17)])
        self.assertEqual(similarity.call_count, 5)
        for call in similarity.call_args_list:
            self.assertEqual(set(call.kwargs), {"min_intersection", "max_aspect_ratio", "log"})
            self.assertEqual(call.kwargs["min_intersection"], TIANJI_MIN_COLOR_INTERSECTION)
            self.assertEqual(call.kwargs["max_aspect_ratio"], TIANJI_MAX_ASPECT_RATIO)
        output = root / "results" / "9.2-嫣然心水" / "天机阁杀料"
        self.assertEqual(len(list(output.glob("*.jpg"))), 12)
        status = load_capture_status(capture_status_path(self.app.status_root, "嫣然心水"))
        self.assertEqual(status["remarks"]["天机阁杀料"]["image_messages"],
                         [{"source": "@yanran", "id": item} for item in range(5, 17)])

    def test_yanran_guaiguai_flow_extends_similar_groups_two_steps(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "嫣然心水", "@yanran")
        self.app.reload_group_profiles("嫣然心水")
        (self.app.program_root / "群配置" / "嫣然心水.json").write_text(
            '{"keywords":["乖乖团队"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")
        preview_calls = []

        def image_bytes(color):
            output = io.BytesIO()
            Image.new("RGB", (720, 1280), color).save(output, format="JPEG")
            return output.getvalue()

        blue, red = image_bytes((70, 180, 230)), image_bytes((210, 40, 35))

        class Message:
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True

            def __init__(self, message_id, caption, group_id, payload):
                self.id, self.raw_text, self.grouped_id = message_id, caption, group_id
                self.payload = payload

            async def download_media(self, file):
                if file is bytes:
                    preview_calls.append(self.id)
                    return self.payload
                Path(file).write_bytes(b"image")
                return file

        messages = [
            Message(1, "乖乖团队", 10, blue),
            Message(2, "", 10, blue),
            Message(3, "", 20, blue),
            Message(4, "", 30, red),
        ]

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, _entity, **_kwargs):
                return iter(messages)

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
        ):
            self.app.start_download()
        self.assertEqual(set(preview_calls), {1, 2, 3, 4})
        output = root / "results" / "9.2-嫣然心水" / "乖乖团队"
        self.assertEqual(len(list(output.glob("*.jpg"))), 3)

    def test_xinao_expert_zhanxia_flow_drops_unwanted_album_by_ocr_marker(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "新澳高手", "@expert")
        self.app.reload_group_profiles("新澳高手")
        (self.app.program_root / "群配置" / "新澳高手.json").write_text(
            '{"keywords":["斩杀系列"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")

        class Message:
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True

            def __init__(self, message_id, caption, group_id, payload):
                self.id, self.raw_text, self.grouped_id = message_id, caption, group_id
                self.payload = payload

            async def download_media(self, file):
                if file is bytes:
                    return self.payload
                Path(file).write_bytes(b"image")
                return file

        messages = [
            Message(1, "斩杀系列", 10, b"unwanted-cover"),
            Message(2, "", 10, b"unwanted-second"),
            Message(3, "斩杀系列", 20, b"wanted-one"),
            Message(4, "", 20, b"wanted-two"),
        ]

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, _entity, **_kwargs):
                return iter(messages)

        def fake_ocr(payload, labels, _engine, on_error=None, cleanup=None):
            return {"实力双波"} if payload == b"unwanted-cover" else set()

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
            patch("telegram_caption_downloader_gui.get_ocr_engine", return_value=object()),
            patch("telegram_caption_downloader_gui.ocr_labels_from_payload", side_effect=fake_ocr),
        ):
            self.app.start_download()
        output = root / "results" / "9.2-新澳高手"
        self.assertEqual(len(list((output / "斩杀系列").glob("*.jpg"))), 2)
        status = load_capture_status(capture_status_path(self.app.status_root, "新澳高手"))
        self.assertEqual(status["remarks"]["斩杀系列"]["status"], "已完成")
        self.assertEqual(status["remarks"]["斩杀系列"]["image_messages"],
                         [{"source": "@expert", "id": 3}, {"source": "@expert", "id": 4}])
        self.assertEqual((output / "未匹配备注.txt").read_text(encoding="utf-8-sig"), "")
        logs = "\n".join(path.read_text(encoding="utf-8") for path in self.app.log_root.rglob("*.log"))
        self.assertIn("实力双波", logs)
        self.assertIn("整组 2 张忽略", logs)

    def test_xinao_expert_ocr_failure_aborts_without_writing_results(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "新澳高手", "@expert")
        self.app.reload_group_profiles("新澳高手")
        (self.app.program_root / "群配置" / "新澳高手.json").write_text(
            '{"keywords":["斩杀系列"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")

        class Message:
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True

            def __init__(self, message_id, caption, group_id):
                self.id, self.raw_text, self.grouped_id = message_id, caption, group_id

            async def download_media(self, file):
                if file is bytes:
                    return b"payload"
                Path(file).write_bytes(b"image")
                return file

        messages = [
            Message(1, "斩杀系列", 10),
            Message(2, "", 10),
            Message(3, "斩杀系列", 20),
            Message(4, "", 20),
        ]

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, _entity, **_kwargs):
                return iter(messages)

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
            patch("telegram_caption_downloader_gui.get_ocr_engine", return_value=None),
        ):
            with self.assertRaises(OcrError):
                self.app.start_download()
        output = root / "results" / "9.2-新澳高手" / "斩杀系列"
        self.assertFalse(output.exists())
        self.assertFalse(capture_status_path(self.app.status_root, "新澳高手").exists())

    def test_huangdaxian_ocr_reads_each_neighbor_first_image_once_without_similarity(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "黄大仙新澳", "@huang")
        self.app.reload_group_profiles("黄大仙新澳")
        labels = ["战狼", "68", "红人馆", "香奈儿"]
        (self.app.program_root / "群配置" / "黄大仙新澳.json").write_text(
            json.dumps({"keywords": labels}, ensure_ascii=False), encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")
        preview_calls = []

        class Message:
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True

            def __init__(self, message_id, caption, group_id, payload):
                self.id, self.raw_text, self.grouped_id = message_id, caption, group_id
                self.payload = payload

            async def download_media(self, file):
                if file is bytes:
                    preview_calls.append(self.id)
                    return self.payload
                Path(file).write_bytes(b"image")
                return file

        messages = [
            Message(1, "", 10, b"hit-68"), Message(2, "", 10, b"ignored"),
            Message(3, "战狼", 20, b"anchor"), Message(4, "", 20, b"anchor"),
            Message(5, "", 30, b"miss"), Message(6, "", 30, b"ignored"),
            Message(7, "香奈儿", 40, b"anchor"), Message(8, "", 40, b"anchor"),
            Message(9, "", 50, b"hit-red"), Message(10, "", 50, b"ignored"),
        ]

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, _entity, **_kwargs):
                return iter(messages)

        def fake_ocr(payload, requested_labels, _engine, on_error=None):
            return {"68"} if payload == b"hit-68" else {"红人馆"} if payload == b"hit-red" else set()

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
            patch("telegram_caption_downloader_gui.get_ocr_engine", return_value=object()),
            patch("telegram_caption_downloader_gui.ocr_labels_from_payload", side_effect=fake_ocr),
            patch("telegram_caption_downloader_gui.image_groups_are_similar") as similarity,
        ):
            self.app.start_download()
        self.assertEqual(preview_calls, [1, 5, 9])
        similarity.assert_not_called()
        output = root / "results" / "9.2-黄大仙新澳"
        self.assertEqual(len(list((output / "战狼").glob("*.jpg"))), 2)
        self.assertEqual(len(list((output / "香奈儿").glob("*.jpg"))), 2)
        self.assertEqual(len(list((output / "68").glob("*.jpg"))), 2)
        self.assertEqual(len(list((output / "红人馆").glob("*.jpg"))), 2)
        self.assertFalse(any(path.name.startswith(f"20260902_120000_{source_tag('@huang')}_5")
                             for path in output.rglob("*.jpg")))

    def test_exact_match_download_flow_writes_report(self):
        class FakeMessage:
            active_downloads = 0
            max_active_downloads = 0

            def __init__(self, message_id, caption, group_id, message_date=None):
                self.id = message_id
                self.raw_text = caption
                self.grouped_id = group_id
                self.date = message_date or datetime(2026, 8, 29, 4, 0, tzinfo=timezone.utc)
                self.file = SimpleNamespace(mime_type="image/jpeg", ext=".jpg")
                self.photo = object()

            async def download_media(self, file):
                type(self).active_downloads += 1
                type(self).max_active_downloads = max(
                    type(self).max_active_downloads,
                    type(self).active_downloads,
                )
                try:
                    await asyncio.sleep(0.01)
                    Path(file).write_bytes(b"image")
                    return file
                finally:
                    type(self).active_downloads -= 1

        messages = [
            FakeMessage(12, "大小姐", None, datetime(2026, 8, 29, 5, 0, tzinfo=timezone.utc)),
            FakeMessage(13, "大小姐", None, datetime(2026, 8, 29, 4, 2, tzinfo=timezone.utc)),
            FakeMessage(10, "陌生人大小姐", 88),
            FakeMessage(11, "", 88),
            SimpleNamespace(
                id=9,
                raw_text="旧消息",
                grouped_id=None,
                date=datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc),
                file=None,
                photo=None,
            ),
        ]

        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_entity(self, _chat):
                return object()

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def iter_messages(self, _entity, offset_date=None):
                return iter(messages)

            def disconnect(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            save_group_profile(
                self.app.program_root,
                self.app.settings,
                "",
                "测试群",
                "@test_group",
                "11:30",
                "12:30",
            )
            self.app.reload_group_profiles("测试群")
            notes = self.app.program_root / "群配置" / "测试群.json"
            notes.write_text(
                json.dumps(
                    {"keywords": ["大小姐", "不存在", "天府统计/天府"]}, ensure_ascii=False
                ),
                encoding="utf-8",
            )
            output = base / "result"
            stale_output = output / "8.29-测试群"
            stale_output.mkdir(parents=True)
            (stale_output / "旧结果.txt").write_text("old", encoding="utf-8")
            self.app.output_path.set(str(output))
            self.app.day.set("2026-08-29")
            self.app.logged_in = True
            self.app.active_account_id = self.account_id
            self.app.active_user_id = 7
            progress_updates = []
            self.app.set_progress = lambda current, total, status, **kwargs: progress_updates.append(
                (current, total, status)
            )
            self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
            with (
                patch("telethon.sync.TelegramClient", FakeClient),
                patch("telegram_caption_downloader_gui.messagebox.showinfo"),
            ):
                self.app.start_download()
            group_output = output / "8.29-测试群"
            tag = source_tag("@test_group")
            self.assertFalse((group_output / "旧结果.txt").exists())
            self.assertTrue((group_output / "大小姐" / f"20260829_120000_{tag}_10.jpg").exists())
            self.assertTrue((group_output / "大小姐" / f"20260829_120000_{tag}_11.jpg").exists())
            self.assertTrue((group_output / "大小姐" / f"20260829_120200_{tag}_13.jpg").exists())
            self.assertFalse((group_output / "大小姐" / f"20260829_130000_{tag}_12.jpg").exists())
            unmatched_lines = (group_output / "未匹配备注.txt").read_text(encoding="utf-8-sig").splitlines()
            self.assertIn("不存在", unmatched_lines)
            self.assertEqual(unmatched_lines.count("天府统计"), 1)
            self.assertTrue((group_output / "提取报告.csv").exists())
            self.assertFalse((output / "提取报告.csv").exists())
            self.assertIn((0, 3, "扫描完成：共发现 3 张图片，符合条件 3 张"), progress_updates)
            completed_updates = [
                status
                for current, total, status in progress_updates
                if current == 3 and total == 3 and status == "正在处理：3 / 3"
            ]
            self.assertEqual(len(completed_updates), 1)
            self.assertRegex(self.app.task_status.get(), r"^完成：3 / 3｜总耗时 \d{2}:\d{2}(?::\d{2})?$")
            self.assertEqual(DOWNLOAD_CONCURRENCY, 14)
            self.assertEqual(FakeMessage.max_active_downloads, 3)
            events = runtime_log_path(self.app.log_root, datetime.now().date(), "测试群").read_text(encoding="utf-8")
            for category in ("复抓", "状态", "扫描", "识别", "下载", "汇总"):
                self.assertIn(f"【{category}】", events)
            self.assertIn("开始链接 1/1：@test_group", events)
            self.assertIn("结束链接 1/1：@test_group", events)
            self.assertIn("网络下载 3，预览复用 0，已有跳过 0，失败 0", events)
            self.assertIn("抓取状态已覆盖保存", events)

            # Same-day retry uses saved state and explains completed-keyword skips.
            with (
                patch("telethon.sync.TelegramClient", FakeClient),
                patch("telegram_caption_downloader_gui.messagebox.showinfo"),
            ):
                self.app.start_download(retry=True)
            events = runtime_log_path(self.app.log_root, datetime.now().date(), "测试群").read_text(encoding="utf-8")
            self.assertIn("同日状态与结果文件夹存在，执行增量复抓", events)
            self.assertIn("已完成跳过（1）：大小姐", events)
            self.assertIn("网络下载 0，预览复用 0，已有跳过 0，失败 0", events)

            from unittest.mock import AsyncMock
            with (
                patch("telethon.sync.TelegramClient", FakeClient),
                patch.object(FakeMessage, "download_media", new=AsyncMock(side_effect=OSError("test disk failure"))),
                patch("telegram_caption_downloader_gui.messagebox.showinfo"),
            ):
                self.app.start_download()
            events = runtime_log_path(self.app.log_root, datetime.now().date(), "测试群").read_text(encoding="utf-8")
            self.assertIn("网络下载 0，预览复用 0，已有跳过 0，失败 3", events)
            self.assertIn("【下载】失败：来源 @test_group 备注 大小姐 消息 10", events)

            with (
                patch("telethon.sync.TelegramClient", FakeClient),
                patch("telegram_caption_downloader_gui.save_capture_status", side_effect=OSError("test state write failure")),
                patch("telegram_caption_downloader_gui.messagebox.showinfo"),
            ):
                with self.assertRaises(OSError):
                    self.app.start_download()
            events = runtime_log_path(self.app.log_root, datetime.now().date(), "测试群").read_text(encoding="utf-8")
            self.assertIn("【状态】抓取状态保存失败：", events)


    def test_same_message_id_from_two_sources_saves_two_files_without_overwrite(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "测试群", "@a | @b")
        self.app.reload_group_profiles("测试群")
        (self.app.program_root / "群配置" / "测试群.json").write_text(
            '{"keywords":["战狼"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")

        class Message:
            grouped_id = None
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True

            def __init__(self, message_id, caption, payload):
                self.id, self.raw_text, self.payload = message_id, caption, payload

            async def download_media(self, file):
                Path(file).write_bytes(self.payload)
                return file

        messages_by_source = {
            "@a": [Message(1000, "战狼", b"image-from-a")],
            "@b": [Message(1000, "战狼", b"image-from-b")],
        }

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, entity, **_kwargs):
                return iter(messages_by_source[entity])

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
        ):
            self.app.start_download()

        group_output = root / "results" / "9.2-测试群"
        file_a = group_output / "战狼" / f"20260902_120000_{source_tag('@a')}_1000.jpg"
        file_b = group_output / "战狼" / f"20260902_120000_{source_tag('@b')}_1000.jpg"
        self.assertEqual(len(list((group_output / "战狼").glob("*.jpg"))), 2)
        self.assertEqual(file_a.read_bytes(), b"image-from-a")
        self.assertEqual(file_b.read_bytes(), b"image-from-b")
        status = load_capture_status(capture_status_path(self.app.status_root, "测试群"))
        self.assertEqual(status["remarks"]["战狼"]["image_count"], 2)
        self.assertEqual(status["remarks"]["战狼"]["image_messages"],
                         [{"source": "@a", "id": 1000}, {"source": "@b", "id": 1000}])
        report_rows = read_report(group_output / "提取报告.csv")
        self.assertEqual(report_rows[0], ["备注", "来源", "消息ID", "北京时间", "本地文件"])
        self.assertEqual([row[1] for row in report_rows[1:]], ["@a", "@b"])
        self.assertEqual([row[2] for row in report_rows[1:]], ["1000", "1000"])

    def test_same_day_retry_queries_each_source_for_its_own_pending_ids(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "测试群", "@a | @b")
        self.app.reload_group_profiles("测试群")
        (self.app.program_root / "群配置" / "测试群.json").write_text(
            '{"keywords":["战狼"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")
        (root / "results" / "9.2-测试群").mkdir(parents=True)
        status_path = capture_status_path(self.app.status_root, "测试群")
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps({
            "group_name": "测试群",
            "date": "2026-09-02",
            "remarks": {"战狼": {"status": "部分完成", "image_count": 0, "image_messages": [],
                                "pending_messages": [{"source": "@a", "id": 7},
                                                     {"source": "@b", "id": 8}]}},
            "last_message_ids": {"@a": 6, "@b": 7},
        }, ensure_ascii=False), encoding="utf-8")

        class Message:
            grouped_id = None
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True

            def __init__(self, message_id, caption):
                self.id, self.raw_text = message_id, caption

            async def download_media(self, file):
                Path(file).write_bytes(f"retry-{self.id}".encode())
                return file

        messages_by_source = {"@a": [Message(7, "战狼")], "@b": [Message(8, "战狼")]}
        pending_queries = []

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, _entity, **_kwargs):
                return iter([])

            def get_messages(self, entity, ids):
                pending_queries.append((entity, list(ids)))
                return [message for message in messages_by_source[entity] if message.id in ids]

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
        ):
            self.app.start_download(retry=True)

        self.assertEqual(pending_queries, [("@a", [7]), ("@b", [8])])
        group_output = root / "results" / "9.2-测试群" / "战狼"
        self.assertEqual(len(list(group_output.glob("*.jpg"))), 2)
        status = load_capture_status(status_path)
        self.assertEqual(status["remarks"]["战狼"]["image_messages"],
                         [{"source": "@a", "id": 7}, {"source": "@b", "id": 8}])
        self.assertEqual(status["remarks"]["战狼"]["pending_messages"], [])

    def test_multi_source_legacy_status_rescans_full_day_and_rewrites_state(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "测试群", "@old | @new")
        self.app.reload_group_profiles("测试群")
        (self.app.program_root / "群配置" / "测试群.json").write_text(
            '{"keywords":["九肖统计"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")
        (root / "results" / "9.2-测试群").mkdir(parents=True)
        status_path = capture_status_path(self.app.status_root, "测试群")
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps({
            "group_name": "测试群",
            "date": "2026-09-02",
            "remarks": {"九肖统计": {"status": "部分完成", "pending_message_ids": [3]}},
            "last_message_ids": {"@old": 5},
        }, ensure_ascii=False), encoding="utf-8")

        pending_queries = []

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, _entity, **_kwargs):
                return iter([])

            def get_messages(self, entity, ids):
                pending_queries.append((entity, list(ids)))
                return []

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
        ):
            self.app.start_download(retry=True)

        self.assertEqual(pending_queries, [])
        events = runtime_log_path(self.app.log_root, datetime.now().date(), "测试群").read_text(encoding="utf-8")
        self.assertIn("状态为旧格式（消息ID未记录来源），多链接群无法判定来源", events)
        self.assertIn("重新扫描当天消息", events)
        self.assertNotIn("pending_message_ids", status_path.read_text(encoding="utf-8"))
        self.assertNotIn("image_message_ids", status_path.read_text(encoding="utf-8"))
        self.assertEqual(load_capture_status(status_path)["remarks"]["九肖统计"]["pending_messages"], [])

    def test_yanran_guaiguai_preview_failure_aborts_instead_of_silently_skipping(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "嫣然心水", "@yanran")
        self.app.reload_group_profiles("嫣然心水")
        (self.app.program_root / "群配置" / "嫣然心水.json").write_text(
            '{"keywords":["乖乖团队"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")

        class Message:
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True

            def __init__(self, message_id, caption, group_id):
                self.id, self.raw_text, self.grouped_id = message_id, caption, group_id

            async def download_media(self, file):
                raise OSError("preview download failure")

        messages = [
            Message(1, "乖乖团队", 10),
            Message(2, "", 10),
            Message(3, "", 20),
        ]

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, _entity, **_kwargs):
                return iter(messages)

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
        ):
            with self.assertRaises(OcrError) as caught:
                self.app.start_download()
        self.assertIn("相似度检查图片缺失", str(caught.exception))
        self.assertIn("本次任务已中止", str(caught.exception))
        self.assertFalse((root / "results" / "9.2-嫣然心水").exists())
        self.assertFalse(capture_status_path(self.app.status_root, "嫣然心水").exists())
        events = runtime_log_path(self.app.log_root, datetime.now().date(), "嫣然心水").read_text(encoding="utf-8")
        self.assertIn("预览下载失败", events)
        self.assertNotIn("完成：", events)


    def test_legacy_multi_source_state_with_all_completed_migrates_to_source_keys(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "测试群", "@old | @new")
        self.app.reload_group_profiles("测试群")
        (self.app.program_root / "群配置" / "测试群.json").write_text(
            '{"keywords":["九肖统计"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")
        (root / "results" / "9.2-测试群").mkdir(parents=True)
        legacy_file = root / "results" / "9.2-测试群" / "九肖统计" / "20260902_120000_1.jpg"
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_file.write_bytes(b"legacy-unknown")
        status_path = capture_status_path(self.app.status_root, "测试群")
        status_path.parent.mkdir(parents=True, exist_ok=True)
        # 所有备注都已完成、且只有旧格式 image_message_ids：旧代码不会重扫，来源与消息ID会丢失。
        status_path.write_text(json.dumps({
            "group_name": "测试群",
            "date": "2026-09-02",
            "remarks": {"九肖统计": {"status": "已完成", "image_count": 2, "image_message_ids": [1, 2]}},
            "last_message_ids": {"@old": 5},
        }, ensure_ascii=False), encoding="utf-8")

        class Message:
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            grouped_id = None

            def __init__(self, message_id, caption):
                self.id, self.raw_text = message_id, caption

            async def download_media(self, file):
                Path(file).write_bytes(f"image-{self.id}".encode())
                return str(file)

        messages_by_source = {"@old": [Message(1, "九肖统计")], "@new": [Message(2, "九肖统计")]}
        pending_queries = []

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, entity, **_kwargs):
                return iter(messages_by_source.get(entity, []))

            def get_messages(self, entity, ids):
                pending_queries.append((entity, list(ids)))
                return []

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
        ):
            self.app.start_download(retry=True)

        self.assertEqual(pending_queries, [])
        label_folder = root / "results" / "9.2-测试群" / "九肖统计"
        files = sorted(path.name for path in label_folder.glob("*.jpg"))
        self.assertEqual(files, [
            f"20260902_120000_{source_tag('@old')}_1.jpg",
            f"20260902_120000_{source_tag('@new')}_2.jpg",
        ])
        self.assertEqual((label_folder / files[0]).read_bytes(), b"image-1")
        self.assertEqual((label_folder / files[1]).read_bytes(), b"image-2")
        # 旧文件无法判定来源：不归给第一个来源，原样移入 _旧文件未映射。
        unmapped = (root / "results" / "9.2-测试群" / "_旧文件未映射" / "九肖统计"
                    / "20260902_120000_1.jpg")
        self.assertTrue(unmapped.is_file())
        self.assertEqual(unmapped.read_bytes(), b"legacy-unknown")
        remark = load_capture_status(status_path)["remarks"]["九肖统计"]
        self.assertEqual(remark["image_count"], 2)
        self.assertEqual(
            {(ref["source"], ref["id"]) for ref in remark["image_messages"]},
            {("@old", 1), ("@new", 2)},
        )
        self.assertEqual(remark["pending_messages"], [])
        self.assertEqual(remark["status"], "已完成")
        state_text = status_path.read_text(encoding="utf-8")
        self.assertNotIn("image_message_ids", state_text)
        self.assertNotIn("pending_message_ids", state_text)
        report_rows = read_report(root / "results" / "9.2-测试群" / "提取报告.csv")
        self.assertEqual(report_rows[0], ["备注", "来源", "消息ID", "北京时间", "本地文件"])
        self.assertEqual([row[1] for row in report_rows[1:]], ["@old", "@new"])
        events = runtime_log_path(self.app.log_root, datetime.now().date(), "测试群").read_text(encoding="utf-8")
        self.assertIn("多链接群无法判定来源", events)
        self.assertIn("重新扫描当天消息", events)

    def test_same_day_retry_migrates_legacy_filename_without_downloading_again(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "测试群", "@test_group")
        self.app.reload_group_profiles("测试群")
        (self.app.program_root / "群配置" / "测试群.json").write_text(
            '{"keywords":["大小姐"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")
        label_folder = root / "results" / "9.2-测试群" / "大小姐"
        label_folder.mkdir(parents=True)
        legacy_file = label_folder / "20260902_120000_11.jpg"
        legacy_file.write_bytes(b"legacy-bytes")
        status_path = capture_status_path(self.app.status_root, "测试群")
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps({
            "group_name": "测试群",
            "date": "2026-09-02",
            "remarks": {"大小姐": {
                "status": "待复抓",
                "image_count": 1,
                "image_messages": [{"source": "@test_group", "id": 10}],
                "pending_messages": [{"source": "@test_group", "id": 11}],
            }},
            "last_message_ids": {"@test_group": 10},
        }, ensure_ascii=False), encoding="utf-8")

        downloads = []

        class Message:
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg", size=12), True
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            grouped_id = None

            def __init__(self, message_id, caption):
                self.id, self.raw_text = message_id, caption

            async def download_media(self, file):
                downloads.append(self.id)
                Path(file).write_bytes(b"downloaded")
                return str(file)

        pending_messages = [Message(11, "大小姐")]

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, _entity, **_kwargs):
                return iter([])

            def get_messages(self, _entity, ids):
                return [message for message in pending_messages if message.id in ids]

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
        ):
            self.app.start_download(retry=True)

        self.assertEqual(downloads, [])
        self.assertEqual(sorted(path.name for path in label_folder.glob("*.jpg")), [
            f"20260902_120000_{source_tag('@test_group')}_11.jpg",
        ])
        self.assertFalse(legacy_file.exists())
        self.assertEqual(next(label_folder.glob("*.jpg")).read_bytes(), b"legacy-bytes")
        remark = load_capture_status(status_path)["remarks"]["大小姐"]
        self.assertEqual(
            {(ref["source"], ref["id"]) for ref in remark["image_messages"]},
            {("@test_group", 10), ("@test_group", 11)},
        )
        self.assertEqual(remark["pending_messages"], [])
        events = runtime_log_path(self.app.log_root, datetime.now().date(), "测试群").read_text(encoding="utf-8")
        self.assertIn("旧文件名迁移", events)

    def test_legacy_multi_source_state_counts_only_images_found_this_run(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "测试群", "@old | @new")
        self.app.reload_group_profiles("测试群")
        (self.app.program_root / "群配置" / "测试群.json").write_text(
            '{"keywords":["九肖统计"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")
        (root / "results" / "9.2-测试群").mkdir(parents=True)
        status_path = capture_status_path(self.app.status_root, "测试群")
        status_path.parent.mkdir(parents=True, exist_ok=True)
        # 旧状态声称有过 2 张（纯数字ID），但来源不明；本次只找回 @old 的那一张。
        status_path.write_text(json.dumps({
            "group_name": "测试群",
            "date": "2026-09-02",
            "remarks": {"九肖统计": {"status": "已完成", "image_count": 2, "image_message_ids": [1, 2]}},
            "last_message_ids": {"@old": 5},
        }, ensure_ascii=False), encoding="utf-8")

        class Message:
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            grouped_id = None

            def __init__(self, message_id, caption):
                self.id, self.raw_text = message_id, caption

            async def download_media(self, file):
                Path(file).write_bytes(f"image-{self.id}".encode())
                return str(file)

        messages_by_source = {"@old": [Message(1, "九肖统计")], "@new": [Message(2, "别的内容")]}

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, entity, **_kwargs):
                return iter(messages_by_source.get(entity, []))

            def get_messages(self, _entity, _ids):
                return []

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
        ):
            self.app.start_download(retry=True)

        remark = load_capture_status(status_path)["remarks"]["九肖统计"]
        self.assertEqual(remark["image_count"], 1)
        self.assertEqual([(ref["source"], ref["id"]) for ref in remark["image_messages"]], [("@old", 1)])
        label_folder = root / "results" / "9.2-测试群" / "九肖统计"
        self.assertEqual(sorted(path.name for path in label_folder.glob("*.jpg")),
                         [f"20260902_120000_{source_tag('@old')}_1.jpg"])

    def test_legacy_multi_source_state_without_found_images_is_not_completed(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "测试群", "@old | @new")
        self.app.reload_group_profiles("测试群")
        (self.app.program_root / "群配置" / "测试群.json").write_text(
            '{"keywords":["九肖统计"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")
        (root / "results" / "9.2-测试群").mkdir(parents=True)
        status_path = capture_status_path(self.app.status_root, "测试群")
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps({
            "group_name": "测试群",
            "date": "2026-09-02",
            "remarks": {"九肖统计": {"status": "已完成", "image_count": 2, "image_message_ids": [1, 2]}},
            "last_message_ids": {"@old": 5},
        }, ensure_ascii=False), encoding="utf-8")

        class Message:
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg"), True
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            grouped_id = None

            def __init__(self, message_id):
                self.id, self.raw_text = message_id, "无关内容"

            async def download_media(self, file):
                Path(file).write_bytes(b"unexpected")
                return str(file)

        messages_by_source = {"@old": [Message(1)], "@new": [Message(2)]}

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, entity, **_kwargs):
                return iter(messages_by_source.get(entity, []))

            def get_messages(self, _entity, _ids):
                return []

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
        ):
            self.app.start_download(retry=True)

        remark = load_capture_status(status_path)["remarks"]["九肖统计"]
        self.assertEqual(remark["image_count"], 0)
        self.assertEqual(remark["image_messages"], [])
        self.assertNotEqual(remark["status"], "已完成")
        self.assertEqual(remark["status"], "未匹配")
        self.assertFalse((root / "results" / "9.2-测试群" / "九肖统计").exists())

    def test_second_same_day_retry_does_not_append_report_rows(self):
        root = Path(self.app_directory.name)
        save_group_profile(self.app.program_root, self.app.settings, "", "测试群", "@test_group")
        self.app.reload_group_profiles("测试群")
        (self.app.program_root / "群配置" / "测试群.json").write_text(
            '{"keywords":["大小姐"]}', encoding="utf-8")
        self.app.output_path.set(str(root / "results"))
        self.app.day.set("2026-09-02")
        (root / "results" / "9.2-测试群").mkdir(parents=True)
        status_path = capture_status_path(self.app.status_root, "测试群")
        status_path.parent.mkdir(parents=True, exist_ok=True)

        def write_pending_state() -> None:
            status_path.write_text(json.dumps({
                "group_name": "测试群",
                "date": "2026-09-02",
                "remarks": {"大小姐": {"status": "待复抓", "image_count": 0, "image_messages": [],
                                       "pending_messages": [{"source": "@test_group", "id": 11}]}},
                "last_message_ids": {"@test_group": 10},
            }, ensure_ascii=False), encoding="utf-8")

        write_pending_state()
        downloads = []

        class Message:
            file, photo = SimpleNamespace(mime_type="image/jpeg", ext=".jpg", size=10), True
            date = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)
            grouped_id = None

            def __init__(self, message_id, caption):
                self.id, self.raw_text = message_id, caption

            async def download_media(self, file):
                downloads.append(self.id)
                Path(file).write_bytes(b"downloaded")
                return str(file)

        pending_messages = [Message(11, "大小姐")]

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                pass

            def disconnect(self):
                pass

            def is_user_authorized(self):
                return True

            def get_me(self):
                return SimpleNamespace(id=7, phone="8613800000000")

            def get_entity(self, address):
                return address

            def iter_messages(self, _entity, **_kwargs):
                return iter([])

            def get_messages(self, _entity, ids):
                return [message for message in pending_messages if message.id in ids]

        self.app.run_worker = lambda operation, success, on_failure=None: success(operation())
        self.app.logged_in, self.app.active_account_id, self.app.active_user_id = True, self.account_id, 7
        report_path = root / "results" / "9.2-测试群" / "提取报告.csv"
        with (
            patch("telethon.sync.TelegramClient", Client),
            patch("telegram_caption_downloader_gui.messagebox.showinfo"),
        ):
            self.app.start_download(retry=True)
            first_rows = read_report(report_path)
            self.assertEqual(len(first_rows), 2)
            # 同日再点一次复抓：消息被重新检查，但图片已在磁盘上，报告不得再追加一行。
            write_pending_state()
            self.app.start_download(retry=True)
            second_rows = read_report(report_path)

        self.assertEqual(second_rows, first_rows)
        self.assertEqual(downloads, [11])
        self.assertEqual(len(list((root / "results" / "9.2-测试群" / "大小姐").glob("*.jpg"))), 1)
        events = runtime_log_path(self.app.log_root, datetime.now().date(), "测试群").read_text(encoding="utf-8")
        self.assertIn("提取报告无新增行", events)


class CaptureRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = GuiFlowTests()
        self.fixture.setUp()
        self.app = self.fixture.app
        self.root = self.fixture.root
        self.account_id = self.fixture.account_id
        self.day = date(2026, 10, 5)

    def tearDown(self):
        dialog = getattr(self.app, "auto_capture_settings_dialog", None)
        if dialog and dialog.window.winfo_exists():
            dialog._close_window()
        self.fixture.tearDown()

    def message(self, message_id, *, fail=False, partial=False):
        message = SimpleNamespace(
            id=message_id, raw_text="known", grouped_id=None, fail=fail, partial=partial, calls=0,
            date=datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc), photo=True,
            file=SimpleNamespace(mime_type="image/jpeg", ext=".jpg", size=len(b"complete-image")),
        )

        async def download_media(file):
            message.calls += 1
            if message.partial:
                Path(file).write_bytes(b"partial")
            if message.fail:
                raise OSError("simulated disconnected download")
            Path(file).write_bytes(b"complete-image")
            return file

        message.download_media = download_media
        return message

    @contextmanager
    def capture(self, messages_by_source, keywords):
        app = self.app
        save_group_profile(app.program_root, app.settings, "", "Recovery", " | ".join(messages_by_source))
        gui.write_notes_config(gui.group_notes_path(app.program_root, "Recovery"), keywords)
        app.reload_group_profiles("Recovery")
        app.output_path.set(str(Path(self.fixture.app_directory.name) / "results"))
        app.day.set(self.day.isoformat())
        app.logged_in = True
        app.selected_account_id = app.active_account_id = self.account_id
        app.active_user_id = 7
        results = []

        class Client:
            def connect(self): pass
            def is_user_authorized(self): return True
            def get_me(self): return SimpleNamespace(id=7, phone="8613800000000")
            def get_entity(self, address): return address
            def iter_messages(self, entity, **kwargs):
                return iter(m for m in messages_by_source[entity] if m.id > kwargs.get("min_id", 0))
            def get_messages(self, entity, ids):
                return [m for m in messages_by_source[entity] if m.id in ids]

        @contextmanager
        def client_context(*_args, **_kwargs):
            yield Client()

        def run_worker(operation, success, on_failure=None):
            result = operation()
            results.append(result)
            success(result)

        app.run_worker = run_worker
        with patch.object(gui, "logged_telegram_client", client_context), patch.object(gui.messagebox, "showinfo"):
            yield results

    def status(self):
        return load_capture_status(capture_status_path(self.app.status_root, "Recovery"))

    def test_manual_capture_always_skips_the_same_day_auto_capture(self):
        self.day = datetime.now(gui.CN_TZ).date()
        message = self.message(1, fail=True)
        message.date = datetime(self.day.year, self.day.month, self.day.day, 12, tzinfo=gui.CN_TZ)
        path = manual_capture_log_path(self.app.status_root)
        with self.capture({"@a": [message]}, ["known"]):
            profile = self.app.settings["groups"][0]
            profile["auto_capture"] = normalize_auto_capture_settings(dict(
                enabled=True, first_time="18:00", second_time="22:00", min_images=1000,
            ))
            # 用户规则：手动抓过就算今天抓过，即使有下载失败也不再自动抓取。
            self.app.start_download()
            self.assertEqual(load_manual_capture_log(path, self.day), {self.day.isoformat(): ["Recovery"]})
            with patch.object(self.app, "_begin_auto_capture") as begin:
                self.app._check_auto_capture_schedule(datetime(self.day.year, self.day.month, self.day.day, 18, tzinfo=gui.CN_TZ))
                begin.assert_not_called()
            message.fail = False
            self.app.start_download(retry=True)
            self.assertEqual(load_manual_capture_log(path, self.day), {self.day.isoformat(): ["Recovery"]})
            with patch.object(self.app, "_begin_auto_capture") as begin:
                self.app._check_auto_capture_schedule(datetime(self.day.year, self.day.month, self.day.day, 22, tzinfo=gui.CN_TZ))
                begin.assert_not_called()

    def test_workflow_mode_does_not_mark_manual_group_complete(self):
        self.day = datetime.now(gui.CN_TZ).date()
        path = manual_capture_log_path(self.app.status_root)
        with self.capture({"@a": []}, ["known"]):
            # 定时工作流跑批不是「手动抓取」，不能占用当天手动抓取标记。
            self.app._workflow_mode = True
            result = gui.CaptureResult(1, 1, 0, 1, Path("unused"), 1, 1)
            try:
                with patch.object(self.app, "run_worker", side_effect=lambda operation, success, **kw: success(result)), \
                        patch.object(self.app, "_workflow_finish"):
                    self.app.start_download()
                    self.root.update()
            finally:
                self.app._workflow_mode = False
            self.assertFalse(path.exists())

    def test_subset_capture_does_not_mark_manual_group_complete(self):
        self.day = datetime.now(gui.CN_TZ).date()
        path = manual_capture_log_path(self.app.status_root)
        with self.capture({"@a": []}, ["known"]):
            # 只复抓部分备注（notes_override）不算当天完整手动抓取。
            result = gui.CaptureResult(1, 1, 0, 1, Path("unused"), 1, 1)
            with patch.object(self.app, "run_worker", side_effect=lambda operation, success, **kw: success(result)):
                self.app.start_download(notes_override={"known": "known"})
                self.root.update()
            self.assertFalse(path.exists())

    def test_all_images_retry_recovers_failure_per_source_with_equal_ids(self):
        first, second = self.message(1, fail=True), self.message(1)
        with self.capture({"@a": [first], "@b": [second]}, []) as results:
            self.app.start_download()
            remark = self.status()["remarks"]["全部图片"]
            self.assertEqual(remark["pending_messages"], [{"source": "@a", "id": 1}])
            self.assertEqual(remark["image_messages"], [{"source": "@b", "id": 1}])
            first.fail = False
            self.app.start_download(retry=True)
            # 失败的那次会先重试一遍（2 次），复抓再成功一次，共 3 次；未失败的来源始终 1 次。
            self.assertEqual((first.calls, second.calls), (3, 1))
            self.assertEqual(results[-1].failed_images, 0)
            self.assertEqual(self.status()["remarks"]["全部图片"]["status"], "已完成")

    def test_old_all_images_status_without_failure_refs_is_reconciled(self):
        message = self.message(1, fail=True)
        with self.capture({"@a": [message]}, []):
            self.app.start_download()
            status = self.status()
            status["remarks"] = {}
            save_capture_status(capture_status_path(self.app.status_root, "Recovery"), status)
            message.fail = False
            self.app.start_download(retry=True)
            self.assertEqual(message.calls, 3)
            self.assertEqual(self.status()["remarks"]["全部图片"]["image_count"], 1)

    def test_failed_download_never_publishes_partial_and_repairs_old_partial_on_retry(self):
        message = self.message(1, fail=True, partial=True)
        with self.capture({"@a": [message]}, ["known"]) as results:
            self.app.start_download()
            output = dated_group_output(Path(self.app.output_path.get()), self.day, "Recovery")
            self.assertEqual(list(output.rglob("*.jpg")), [])
            self.assertEqual(list(output.rglob("*.part")), [])
            self.assertEqual(results[-1].failed_images, 1)
            # Emulate a truncated formal file left by the old version.
            old = output / "known" / f"20261005_120000_{source_tag('@a')}_1.jpg"
            old.write_bytes(b"partial")
            message.fail = message.partial = False
            self.app.start_download(retry=True)
            self.assertEqual(message.calls, 3)
            self.assertEqual(old.read_bytes(), b"complete-image")
            self.assertEqual(self.status()["remarks"]["known"]["status"], "已完成")

    def test_short_download_return_value_is_not_treated_as_success(self):
        message = self.message(1)
        async def short_download(file):
            Path(file).write_bytes(b"short")
            return file
        message.download_media = short_download
        with self.capture({"@a": [message]}, ["known"]) as results:
            self.app.start_download()
            self.assertEqual(results[-1].failed_images, 1)
            self.assertEqual(results[-1].downloaded, 0)
            self.assertEqual(self.status()["remarks"]["known"]["status"], "待复抓")

    def test_auto_threshold_met_completes_even_with_failed_downloads(self):
        messages = [self.message(1), self.message(2, fail=True)]
        with self.capture({"@a": messages}, ["known"]):
            profile = self.app.settings["groups"][0]
            config = normalize_auto_capture_settings(dict(
                enabled=True, first_time="18:00", second_time="22:00", min_images=1,
            ))
            self.app._begin_auto_capture(profile, config, 1, self.day)
            # 用户规则：图片数达标即算今日完成，第二次抓取只服务于「没达到最低图片数」。
            self.assertEqual(profile["auto_capture"]["captured_date"], self.day.isoformat())
            self.assertEqual(profile["auto_capture"]["threshold_met_date"], self.day.isoformat())
            self.assertIn("下载失败 1 张", self.app.task_status.get())
            self.assertIsNone(auto_capture_due(profile["auto_capture"], datetime(2026, 10, 5, 22, 0)))
            self.assertEqual(self.status()["remarks"]["known"]["status"], "部分完成")

    def test_bad_notes_preserve_all_groups_and_bad_groups_cannot_be_overwritten(self):
        for bad_groups in (False, True):
            with self.subTest(bad_groups=bad_groups):
                base = Path(self.fixture.app_directory.name) / str(bad_groups)
                program = base / "program"
                settings = gui._default_group_settings()
                save_group_profile(program, settings, "", "Preserve", "@preserve")
                group_file = program / "群配置" / "groups.json"
                notes_file = program / "群配置" / "Preserve.json"
                (group_file if bad_groups else notes_file).write_text('{"broken":', encoding="utf-8")
                before_group, before_notes = group_file.read_bytes(), notes_file.read_bytes()
                root = Tk()
                root.withdraw()
                try:
                    app = TelegramDownloaderApp(root, app_data=base / "data", program_root=program,
                                                status_root=base / "status", show_account_dialog=False)
                    root.update()
                    self.assertEqual(group_file.read_bytes(), before_group)
                    self.assertEqual(notes_file.read_bytes(), before_notes)
                    if bad_groups:
                        with self.assertRaises(ValueError):
                            gui._write_group_settings(program, app.settings)
                    else:
                        self.assertEqual(len(app.settings["groups"]), 1)
                    self.assertIn("读取失败", app.task_status.get())
                finally:
                    root.destroy()

    def test_busy_settings_show_reason_keep_draft_and_preserve_threshold_marker(self):
        with self.capture({"@a": []}, []):
            profile = self.app.settings["groups"][0]
            config = normalize_auto_capture_settings(dict(
                enabled=True, first_time="18:00", second_time="22:00", min_images=1000,
                first_attempt_date=self.day.isoformat(), threshold_met_date=self.day.isoformat(),
            ))
            gui.save_auto_capture_settings(self.app.program_root, self.app.settings, profile, config)
            self.app.open_auto_capture_settings()
            self.root.update()
            dialog = self.app.auto_capture_settings_dialog
            dialog.min_images.set("900")
            self.app.set_busy(True)
            self.assertTrue(dialog.save_button.instate(["disabled"]))
            self.assertIn("暂不能保存", dialog.save_feedback.get())
            dialog.save_button.invoke()
            self.assertEqual(profile["auto_capture"]["min_images"], 1000)
            self.app._auto_capture_context = dict(group_name="Recovery", target_day=self.day, minimum=1000, attempt=1)
            self.app._finish_auto_capture(gui.CaptureResult(2, 1, 1, 2, Path("unused"), 1.0, 1000, False, 1))
            self.root.update()
            self.assertEqual(dialog.min_images.get(), "900")
            self.app.set_busy(False)
            self.assertFalse(dialog.save_button.instate(["disabled"]))
            dialog.save_button.invoke()
            saved = load_group_settings(self.app.program_root)["groups"][0]["auto_capture"]
            self.assertEqual(saved["min_images"], 900)
            self.assertEqual(saved["threshold_met_date"], self.day.isoformat())
            self.assertIn("已保存", dialog.save_feedback.get())


    def test_subset_retry_keeps_other_downloads_and_report_rows(self):
        known, other = self.message(1), self.message(2)
        other.raw_text = "other"
        with self.capture({"@a": [known, other]}, ["known", "other"]):
            self.app.start_download()
            output = dated_group_output(Path(self.app.output_path.get()), self.day, "Recovery")
            other_file = next((output / "other").glob("*.jpg"))
            before_rows = read_report(output / "提取报告.csv")
            self.assertEqual(len(before_rows), 3)

            self.app.start_download(notes_override={"known": "known"}, retry=True)

            # 指定备注复抓只补这些备注，绝不能删掉当天其它备注的图片与报告行。
            self.assertTrue(other_file.is_file())
            after_rows = read_report(output / "提取报告.csv")
            self.assertEqual(len(after_rows), len(before_rows))
            self.assertEqual([row[0] for row in after_rows[1:]], ["known", "other"])
            keys = [tuple(row) for row in after_rows[1:]]
            self.assertEqual(len(keys), len(set(keys)))


    def test_extract_report_merges_path_rewrite_without_duplicating_the_image(self):
        group_output = Path(self.fixture.app_directory.name) / "报告合并"
        folder = group_output / "战狼"
        folder.mkdir(parents=True)
        legacy_name = "20261005_120000_1.jpg"
        new_name = "20261005_120000_abcd1234_1.jpg"
        report = group_output / "提取报告.csv"
        report.write_text(
            "备注,消息ID,北京时间,本地文件\n"
            f"战狼,1,2026-10-05T12:00:00+08:00,{folder / legacy_name}\n",
            encoding="utf-8-sig",
        )
        migrated = {("战狼", legacy_name): str(folder / new_name)}
        rows = [["战狼", "@a", 1, "2026-10-05T12:00:00+08:00", str(folder / new_name)]]

        self.app._write_extract_report(report, group_output, rows, False, True, migrated)

        result = read_report(report)
        self.assertEqual(len(result), 2)  # 表头 + 1 行
        self.assertEqual(result[1][1], "@a")
        self.assertTrue(result[1][4].endswith(new_name))
        self.assertEqual(len({tuple(row) for row in result[1:]}), 1)

    def test_extract_report_keeps_legacy_report_when_no_new_rows(self):
        group_output = Path(self.fixture.app_directory.name) / "旧报告保留"
        folder = group_output / "战狼"
        folder.mkdir(parents=True)
        report = group_output / "提取报告.csv"
        report.write_text(
            "备注,消息ID,北京时间,本地文件\n"
            f"战狼,1,2026-10-05T12:00:00+08:00,{folder / 'a.jpg'}\n",
            encoding="utf-8-sig",
        )

        # 没有本次新行：报告必须仍被写回（升级成 5 列），不能被搬成备份后就消失。
        self.app._write_extract_report(report, group_output, [], False, True, {})

        self.assertTrue(report.is_file())
        result = read_report(report)
        self.assertEqual(result[0], list(gui.REPORT_HEADER))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1][1], "")
        self.assertEqual(result[1][2], "1")
        backups = list(group_output.glob("提取报告-旧格式备份-*.csv"))
        self.assertEqual(len(backups), 1)

    def test_extract_report_keeps_legacy_report_when_write_fails(self):
        group_output = Path(self.fixture.app_directory.name) / "报告写失败"
        folder = group_output / "战狼"
        folder.mkdir(parents=True)
        report = group_output / "提取报告.csv"
        original = (
            "备注,消息ID,北京时间,本地文件\n"
            f"战狼,1,2026-10-05T12:00:00+08:00,{folder / 'a.jpg'}\n"
            f"68,2,2026-10-05T12:00:01+08:00,{folder / 'b.jpg'}\n"
        )
        report.write_text(original, encoding="utf-8-sig")
        rows = [["战狼", "@a", 9, "2026-10-05T13:00:00+08:00", str(folder / "c.jpg")]]

        with patch.object(gui, "_atomic_write_bytes", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.app._write_extract_report(report, group_output, rows, False, True, {})

        # 写盘失败时原报告必须还在（备份是复制，不是搬家）。
        self.assertTrue(report.is_file())
        self.assertEqual(report.read_text(encoding="utf-8-sig"), original)

    def test_report_write_failure_does_not_fail_the_capture(self):
        message = self.message(1)
        with self.capture({"@a": [message]}, ["known"]) as results:
            self.app.start_download()
            with patch.object(gui, "read_report_rows", side_effect=OSError("locked by excel")):
                self.app.start_download(retry=True)
            self.assertEqual(len(results), 2)
            self.assertEqual(results[-1].failed_images, 0)
            # 报告写不了，但抓取状态仍要落盘。
            self.assertEqual(self.status()["remarks"]["known"]["status"], "已完成")

    def test_run_worker_reports_when_a_task_is_already_running(self):
        self.app.set_busy(True)
        try:
            self.assertFalse(self.app.run_worker(lambda: 1, lambda _result: None))
        finally:
            self.app.set_busy(False)

    def test_run_worker_always_releases_the_ui_after_an_unhandled_exit(self):
        class ImmediateThread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

        def boom():
            raise KeyboardInterrupt()

        with patch("telegram_caption_downloader_gui.threading.Thread", ImmediateThread):
            self.assertTrue(self.app.run_worker(boom, lambda _result: None))
        self.root.update()
        self.assertFalse(self.app._busy)


class RiskFixRegressionTests(unittest.TestCase):
    """回归测试：P0–P2 风险修复后的新行为。"""

    @staticmethod
    def _png_bytes():
        payload = io.BytesIO()
        Image.new("RGB", (4, 4), (255, 255, 255)).save(payload, format="PNG")
        return payload.getvalue()

    def test_parse_clock_rejects_non_text_instead_of_crashing(self):
        for value in (None, 1030, ["18:00"]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_clock(value)

    def test_ocr_short_label_does_not_match_inside_longer_number(self):
        class FakeOCR:
            def __init__(self, *lines):
                self.lines = list(lines)

            def predict(self, image):
                return [{"rec_texts": self.lines}]

        payload = self._png_bytes()
        self.assertEqual(ocr_labels_from_payload(payload, {"68"}, FakeOCR("168码中特")), set())
        self.assertEqual(ocr_labels_from_payload(payload, {"68"}, FakeOCR("第68码")), {"68"})
        self.assertEqual(ocr_labels_from_payload(payload, {"五肖"}, FakeOCR("十五肖统计")), set())
        self.assertEqual(ocr_labels_from_payload(payload, {"五肖"}, FakeOCR("五肖统计")), {"五肖"})

    def test_ocr_labels_do_not_span_two_lines(self):
        class FakeOCR:
            def predict(self, image):
                return [{"rec_texts": ["小数", "双数"]}]

        self.assertEqual(ocr_labels_from_payload(self._png_bytes(), {"小数双数"}, FakeOCR()), set())

    def test_auto_capture_started_after_second_time_runs_only_attempt_two(self):
        config = normalize_auto_capture_settings(dict(
            enabled=True, first_time="18:00", second_time="22:30", min_images=1000,
        ))
        late = datetime(2026, 10, 3, 23, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(auto_capture_due(config, late), 2)

    def test_group_settings_reject_duplicate_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "群配置").mkdir(parents=True, exist_ok=True)
            (root / "群配置" / "groups.json").write_text(json.dumps({
                "groups": [
                    {"name": "同名群", "address": "@a", "start_time": "00:00", "end_time": "23:59"},
                    {"name": "同名群", "address": "@b", "start_time": "00:00", "end_time": "23:59"},
                ],
                "selected_group": "同名群",
                "output_path": str(root / "结果"),
            }, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_group_settings(root)

    def test_label_folder_names_reject_collisions(self):
        self.assertEqual(gui._label_folder_names(["战狼"])["战狼"], "战狼")
        with self.assertRaises(ValueError):
            gui._label_folder_names(["A/B", "A:B"])

    def test_read_report_keys_raises_instead_of_silently_returning_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "提取报告.csv"
            path.write_text("备注,来源,消息ID,北京时间,本地文件\n战狼,@a,1,2026-10-05T12:00:00+08:00,C:/x.jpg\n",
                            encoding="utf-8-sig")
            self.assertEqual(len(gui.read_report_keys(path)), 1)
            self.assertTrue(gui.report_header_is_current(path))
            with patch.object(Path, "open", side_effect=PermissionError("locked")):
                with self.assertRaises(OSError):
                    gui.read_report_keys(path)

    def test_report_header_detects_old_four_column_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "提取报告.csv"
            path.write_text("备注,消息ID,北京时间,本地文件\n战狼,1,2026-10-05T12:00:00+08:00,C:/x.jpg\n",
                            encoding="utf-8-sig")
            self.assertFalse(gui.report_header_is_current(path))

    def test_truncated_image_without_known_size_is_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.jpg"
            payload = self._png_bytes()
            path.write_bytes(payload[:12])
            message = SimpleNamespace(file=SimpleNamespace(size=None))
            self.assertFalse(gui.downloaded_file_is_complete(path, message))
            path.write_bytes(payload)
            self.assertTrue(gui.downloaded_file_is_complete(path, message))


    def test_read_report_rows_drops_header_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "提取报告.csv"
            path.write_text(
                "备注,来源,消息ID,北京时间,本地文件\n"
                "战狼,@a,1,2026-10-05T12:00:00+08:00,C:/a.jpg\n"
                "68,@b,2,2026-10-05T12:00:01+08:00,C:/b.jpg\n",
                encoding="utf-8-sig",
            )
            rows = gui.read_report_rows(path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][0], "战狼")
            self.assertEqual(gui.read_report_rows(Path(directory) / "不存在.csv"), [])


    def test_ocr_short_label_still_matches_next_to_a_longer_number(self):
        class FakeOCR:
            def __init__(self, *lines):
                self.lines = list(lines)

            def predict(self, image):
                return [{"rec_texts": self.lines}]

        payload = self._png_bytes()
        # 只有“贴着的那一侧本身是数字/中文数字”才算粘连，正常牌面不能被判成不命中。
        self.assertEqual(ocr_labels_from_payload(payload, {"五肖"}, FakeOCR("五肖六码")), {"五肖"})
        self.assertEqual(ocr_labels_from_payload(payload, {"战狼"}, FakeOCR("战狼168")), {"战狼"})
        self.assertEqual(ocr_labels_from_payload(payload, {"战狼"}, FakeOCR("168战狼")), {"战狼"})
        self.assertEqual(ocr_labels_from_payload(payload, {"九肖"}, FakeOCR("九肖十码")), {"九肖"})

    def test_auto_capture_marks_today_when_attempt_two_runs_first(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = {"groups": [{"name": "自动群", "address": "@a", "start_time": "00:00", "end_time": "23:59"}],
                        "selected_group": "自动群", "output_path": str(root / "结果")}
            profile = settings["groups"][0]
            config = normalize_auto_capture_settings(dict(
                enabled=True, first_time="18:00", second_time="22:30", min_images=1000,
                first_attempt_date="2026-10-17",
            ))
            gui.save_auto_capture_settings(root, settings, profile, config)

            app = SimpleNamespace(
                program_root=root, settings=settings, log=lambda _text: None,
                start_download=lambda **_kwargs: None,
            )
            app._auto_capture_context = None
            TelegramDownloaderApp._begin_auto_capture(app, profile, config, 2, date(2026, 10, 18))

            saved = load_group_settings(root)["groups"][0]["auto_capture"]
            # 必须无条件写成今天，否则前一天残留的 first_attempt_date 会让每轮轮询反复触发。
            self.assertEqual(saved["first_attempt_date"], "2026-10-18")
            self.assertEqual(saved["second_attempt_date"], "2026-10-18")
            late = datetime(2026, 10, 18, 23, 0, tzinfo=timezone(timedelta(hours=8)))
            self.assertIsNone(auto_capture_due(saved, late))

    def test_default_paths_fall_back_to_project_root(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "已清理的旧目录"
            with patch.object(gui, "LEGACY_WORKSPACE", missing):
                self.assertEqual(gui.default_output_path(), gui.project_root() / "结果")
                self.assertEqual(gui.default_status_root(), gui.project_root() / "outputs" / "抓取状态")
                # 不能出现 outputs\outputs 这种多一层的错误回退。
                self.assertNotIn(gui.project_root(), gui.default_status_root().parents[1].parents)

    def test_truncated_jpeg_without_known_size_is_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.jpg"
            payload = io.BytesIO()
            Image.new("RGB", (400, 400), (200, 30, 30)).save(payload, format="JPEG")
            data = payload.getvalue()
            path.write_bytes(data)
            message = SimpleNamespace(file=SimpleNamespace(size=None))
            self.assertTrue(gui.downloaded_file_is_complete(path, message))
            path.write_bytes(data[: int(len(data) * 0.6)])
            self.assertFalse(gui.downloaded_file_is_complete(path, message))


    def test_ocr_label_does_not_match_punctuation_joined_digits(self):
        class FakeOCR:
            def __init__(self, *lines):
                self.lines = list(lines)

            def predict(self, image):
                return [{"rec_texts": self.lines}]

        payload = self._png_bytes()
        # 顿号/逗号/小数点隔开的数字不是 68，不能被拼成标签。
        self.assertEqual(ocr_labels_from_payload(payload, {"68"}, FakeOCR("6、8码")), set())
        self.assertEqual(ocr_labels_from_payload(payload, {"68"}, FakeOCR("6,8码")), set())
        self.assertEqual(ocr_labels_from_payload(payload, {"68"}, FakeOCR("6.8码")), set())
        # 原本就有空白的真命中要能救回
        self.assertEqual(ocr_labels_from_payload(payload, {"68"}, FakeOCR("1 68码")), {"68"})


if __name__ == "__main__":
    unittest.main()
