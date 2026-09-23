#!/usr/bin/env python3
"""Telegram 图片备注精准提取器（Windows GUI）。"""

from __future__ import annotations

import csv
import asyncio
import ctypes
import io
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import webbrowser
from uuid import uuid4
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from tkinter import BooleanVar, Frame, StringVar, TclError, Tk, Toplevel, filedialog, messagebox, simpledialog
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from ctypes import wintypes

from PIL import Image, UnidentifiedImageError


CN_TZ = timezone(timedelta(hours=8))
APP_DATA = (
    (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
     else Path(__file__).resolve().parent).parent
    / "运行数据"
)
CRYPTPROTECT_UI_FORBIDDEN = 0x1
APP_VERSION = "v5.2.9"
GROUP_DIR_NAME = "群配置"
GROUP_SETTINGS_NAME = "groups.json"
DEFAULT_OUTPUT_PATH = Path(r"C:\Users\Administrator\Desktop\每天工具\飞机抓图\结果")
DOWNLOAD_CONCURRENCY = 6
SPECIAL_RETRY_GROUP = "嫣然心水"
SPECIAL_ADJACENT_LABEL = "乖乖团队"
YANRAN_ADJACENT_LABELS = {"乖乖团队", "天机阁特围", "天机阁杀料", "恩平"}
YANRAN_FIRST_IMAGE_OCR_LABELS = {"天机阁特围", "天机阁杀料"}
# Calibrated against the 2026-09-23 Tianji images and other same-day categories.
TIANJI_MIN_COLOR_INTERSECTION = 0.70
TIANJI_MAX_ASPECT_RATIO = 1.20
VISUAL_ADJACENT_EXTENSION_GROUPS = 2
HUANGDAXIAN_GROUP = "黄大仙新澳"
HUANGDAXIAN_OCR_LABELS = {"战狼", "68", "红人馆", "香奈儿"}
HUANGDAXIAN_ADJACENT_LABELS = HUANGDAXIAN_OCR_LABELS
MUXI_GROUP = "慕熙会员群"
MUXI_EXACT_LABELS = {"九肖", "绝杀合数", "帅铁精杀", "四头中特24码中特", "大围"}
XINAO_EXPERT_GROUP = "新澳高手"
XINAO_EXPERT_FILTER_LABELS = {"斩杀系列"}
XINAO_EXPERT_UNWANTED_MARKERS = {
    "实力双波", "天地中特", "三行中特", "三头必中", "成语解平特",
    "平特一肖", "赚钱六肖", "五肖", "五码", "小数+双数",
}
STATUS_OUTPUT_DIR = Path(r"C:\Users\Administrator\Desktop\每天工具\飞机抓图\outputs\抓取状态")
DARK_BG = "#161719"
DARK_SURFACE = "#1B1D20"
DARK_FIELD = "#222428"
DARK_FIELD_RO = "#1E2124"
DARK_BORDER = "#33363B"
DARK_BORDER_SOFT = "#26292D"
DARK_TEXT = "#E8EAED"
DARK_MUTED = "#9AA0A6"
DARK_DIM = "#6E747B"
DARK_ACCENT = "#E8A33D"
DARK_ACCENT_SOFT = "#3A301C"
DARK_DANGER = "#E0715B"
DARK_SUCCESS = "#7CC47F"
DARK_LOG_BG = "#111214"
DARK_BOTTOM = "#121315"
FORM_FONT = ("Microsoft YaHei UI", 13)
RUNTIME_LOG_DIR_NAME = "运行日志"
UI_SETTINGS_NAME = "ui_settings.json"
WINDOW_PRESETS = {"2k": (1440, 860), "1080": (1440, 860)}
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))
}
UI_HEARTBEAT_SECONDS = 1.0
UI_FREEZE_SECONDS = 5.0
UI_FREEZE_DUMP_COOLDOWN = 30.0


def thread_stack_dump() -> str:
    """Snapshot every live thread, so a frozen UI thread can still be diagnosed afterwards."""
    frames = sys._current_frames()
    blocks = []
    for thread in threading.enumerate():
        frame = frames.get(thread.ident)
        header = f"--- 线程 {thread.name}（daemon={thread.daemon}）"
        blocks.append(header if frame is None else header + "\n" + "".join(traceback.format_stack(frame)))
    return "\n".join(blocks)


class TelegramRuntimeLogHandler(logging.Handler):
    """Forward operational events only; never dump Telegram requests or responses."""

    def __init__(self, write_log):
        super().__init__(logging.INFO)
        self.write_log = write_log

    def emit(self, record):
        template = str(record.msg)
        lower = template.lower()
        try:
            if template == 'Sleeping%s for %ds (%s) on %s flood wait':
                _, seconds, _, request = record.args
                text = f"【限流】FLOOD_WAIT：自动等待 {int(seconds)} 秒后重试；请求 {request}"
            elif "flood" in lower:
                text = "【限流】底层连接收到限流通知（未提供可用等待秒数）"
            elif "timeout" in lower:
                text = "【网络】下载超时；" + ("连续超时，本次请求失败" if "two timeouts" in lower else "组件准备重试")
            elif "automatic reconnection failed" in lower:
                text = "【网络】自动重连失败，已用尽本轮重连次数"
            elif "failed reconnection attempt" in lower or "at connecting failed" in lower:
                text = "【网络】本次连接尝试失败"
            elif "reconnect" in lower:
                text = "【网络】连接中断，准备自动重连"
            elif "connection closed" in lower:
                text = "【网络】传输期间连接已断开"
            elif template.startswith("Connection to") and "complete" in lower:
                text = "【网络】连接建立成功（主连接或下载连接）"
            elif template.startswith("Connecting to"):
                text = "【网络】正在建立连接"
            elif template.startswith("Disconnection from"):
                text = "【网络】连接已关闭"
            elif "telegram is having internal issues" in lower:
                text = "【网络】Telegram 服务端异常，组件准备重试"
            elif "file ref expired" in lower:
                text = "【网络】文件引用过期，重新读取消息后继续下载"
            elif record.levelno >= logging.WARNING:
                # Deliberately omit args/exception text: these can contain session material.
                text = f"【网络】底层组件告警（{record.levelname}；{record.name.rsplit('.', 1)[-1]}）"
            else:
                return
            self.write_log(text)
        except Exception:
            # A logging/UI failure must not turn a successful request into a failed download.
            pass


@contextmanager
def logged_telegram_client(session, api_id, api_hash, write_log):
    from telethon.sessions import Session
    from telethon.sync import TelegramClient

    handler = TelegramRuntimeLogHandler(write_log)
    owner = getattr(write_log, "__self__", write_log)
    logger = logging.getLogger(f"telegram_downloader.runtime.{id(owner)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        # Suppress late events without retaining the UI callback after disconnect.
        logger.addHandler(logging.NullHandler())
    logger.addHandler(handler)
    client = None
    try:
        resolved = session if isinstance(session, Session) else load_account_session(session, write_log)
        client = TelegramClient(
            resolved, api_id, api_hash, base_logger=logger,
            timeout=15, connection_retries=2, retry_delay=1,
        )
        yield client
    finally:
        try:
            if client is not None:
                client.disconnect()
        finally:
            logger.removeHandler(handler)
            handler.close()


def media_group_description(group) -> str:
    first = group[0]
    return (f"来源ID={getattr(first, 'chat_id', None)} 相册={first.grouped_id or '单图'} "
            f"消息={','.join(str(message.id) for message in group)}")


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _data_blob(data: bytes) -> tuple[DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    input_blob, input_buffer = _data_blob(data)
    output_blob = DataBlob()
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if protect:
        succeeded = function(
            ctypes.byref(input_blob),
            "TelegramPhotoExtractor",
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    else:
        succeeded = function(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    del input_buffer
    if not succeeded:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def save_saved_credentials(path: Path, api_id: int, api_hash: str, phone: str) -> None:
    if api_id <= 0 or not api_hash or not phone:
        raise ValueError("登录配置不完整")
    plaintext = json.dumps(
        {"api_id": api_id, "api_hash": api_hash, "phone": phone},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    encrypted = _dpapi(plaintext, protect=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encrypted)
    os.replace(temporary, path)


def load_saved_credentials(path: Path) -> tuple[int, str, str] | None:
    if not path.is_file():
        return None
    payload = json.loads(_dpapi(path.read_bytes(), protect=False).decode("utf-8"))
    api_id, api_hash, phone = payload.get("api_id"), payload.get("api_hash"), payload.get("phone")
    if not isinstance(api_id, int) or api_id <= 0 or not isinstance(api_hash, str) or not isinstance(phone, str):
        raise ValueError("保存的登录配置无效")
    if not api_hash or not phone:
        raise ValueError("保存的登录配置不完整")
    return api_id, api_hash, phone


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def normalize_account_phone(phone: str) -> str:
    phone = re.sub(r"[\s()\-]", "", phone)
    if not re.fullmatch(r"\+?[1-9][0-9]{6,14}", phone):
        raise ValueError("手机号需包含国家区号，例如 +86…")
    return "+" + phone.lstrip("+")


def account_directory(app_data: Path, account_id: str) -> Path:
    if account_id != "legacy" and not re.fullmatch(r"[0-9a-f]{32}", account_id):
        raise ValueError("账号标识无效")
    root = Path(app_data).resolve()
    directory = root if account_id == "legacy" else root / "accounts" / account_id
    if not directory.resolve().is_relative_to(root):
        raise ValueError("账号保存路径无效")
    return directory


SESSION_BLOB_NAME = "session.bin"
SESSION_LOCKED_TEXT = "账号会话文件被占用，请关闭其他软件实例或重启软件后再试"
SESSION_BROKEN_TEXT = "账号会话文件无法读取，请重新登录该账号"


def session_blob_path(directory: Path) -> Path:
    return Path(directory) / SESSION_BLOB_NAME


def save_session_string(path: Path, value: str) -> None:
    encrypted = _dpapi(value.encode("utf-8"), protect=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encrypted)
    os.replace(temporary, path)


def load_session_string(path: Path) -> str | None:
    path = Path(path)
    if not path.is_file():
        return None
    try:
        return _dpapi(path.read_bytes(), protect=False).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(SESSION_BROKEN_TEXT) from exc


def has_account_session(directory: Path) -> bool:
    directory = Path(directory)
    return session_blob_path(directory).is_file() or (directory / "account.session").is_file()


def load_account_session(session_path: Path, write_log=None):
    from telethon.sessions import SQLiteSession, StringSession

    session_path = Path(session_path)
    directory = session_path.parent
    saved = load_session_string(session_blob_path(directory))
    if saved:
        return StringSession(saved)
    legacy = Path(str(session_path) + ".session")
    if not legacy.is_file():
        return StringSession()
    try:
        old = SQLiteSession(str(session_path))
        try:
            value = StringSession.save(old)
        finally:
            old.close()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(SESSION_LOCKED_TEXT) from exc
    except (OSError, sqlite3.DatabaseError) as exc:
        raise ValueError(SESSION_BROKEN_TEXT) from exc
    save_session_string(session_blob_path(directory), value)
    if write_log is not None:
        write_log("【账号】旧会话已迁移为加密字符串，原 .session 文件保留")
    return StringSession(value)


def load_account_profiles(app_data: Path) -> list[dict]:
    path = Path(app_data) / "accounts.json"
    profiles = []
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), list):
            raise ValueError("账号列表格式无效，原文件已保留")
        seen = set()
        for item in payload["accounts"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError("账号列表格式无效")
            account_directory(app_data, item["id"])
            name, user_id = item.get("name"), item.get("user_id")
            if (item["id"] in seen or not isinstance(name, str) or not name.strip() or len(name) > 80
                    or (user_id is not None and (type(user_id) is not int or user_id <= 0))):
                raise ValueError("账号列表格式无效")
            seen.add(item["id"])
            profiles.append({"id": item["id"], "name": name, "user_id": user_id})
    # The original session stays in place, including its SQLite journal/WAL files.
    if (Path(app_data) / "credentials.bin").exists() and not any(p["id"] == "legacy" for p in profiles):
        profiles.insert(0, {"id": "legacy", "name": "原有账号", "user_id": None})
    return profiles


def save_account_profile(app_data: Path, profile: dict) -> None:
    profiles = load_account_profiles(app_data)
    profiles = [profile if item["id"] == profile["id"] else item for item in profiles]
    if not any(item["id"] == profile["id"] for item in profiles):
        profiles.append(profile)
    path = Path(app_data) / "accounts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"accounts": profiles}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_selected_account(app_data: Path) -> str:
    path = Path(app_data) / "account_selection.json"
    if not path.exists():
        return ""
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    account_id = payload.get("account_id") if isinstance(payload, dict) else None
    if not isinstance(account_id, str) or not any(item["id"] == account_id for item in load_account_profiles(app_data)):
        raise ValueError("本地默认账号记录无效或账号已不存在，请手动选择；原文件已保留")
    return account_id


def save_selected_account(app_data: Path, account_id: str) -> None:
    if not any(item["id"] == account_id for item in load_account_profiles(app_data)):
        raise ValueError("不能保存不存在的账号")
    path = Path(app_data) / "account_selection.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"account_id": account_id}), encoding="utf-8")
    os.replace(temporary, path)


def load_window_preset(app_data: Path) -> str:
    path = Path(app_data) / UI_SETTINGS_NAME
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    preset = payload.get("window_preset") if isinstance(payload, dict) else None
    return preset if preset in WINDOW_PRESETS else ""


def save_window_preset(app_data: Path, preset: str) -> None:
    if preset not in WINDOW_PRESETS:
        raise ValueError("窗口分辨率选项无效")
    path = Path(app_data) / UI_SETTINGS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"window_preset": preset}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def create_account_profile(app_data: Path, name: str, api_id: int, api_hash: str, phone: str) -> dict:
    name = normalized(name)
    if not name or len(name) > 80:
        raise ValueError("账号名称须为 1～80 个字")
    phone = normalize_account_phone(phone)
    for profile in load_account_profiles(app_data):
        saved = load_saved_credentials(account_directory(app_data, profile["id"]) / "credentials.bin")
        if saved and normalize_account_phone(saved[2]) == phone:
            raise ValueError("这个手机号已保存，请从账号列表选择，不要重复添加")
    profile = {"id": uuid4().hex, "name": name, "user_id": None}
    directory = account_directory(app_data, profile["id"])
    save_saved_credentials(directory / "credentials.bin", api_id, api_hash, phone)
    save_account_profile(app_data, profile)
    return profile


def verify_account_user(profile: dict, user, phone: str) -> None:
    user_id = getattr(user, "id", None)
    actual_phone = getattr(user, "phone", None)
    if not user_id or (profile.get("user_id") is not None and profile["user_id"] != user_id):
        raise RuntimeError("当前会话与所选账号不一致，已停止操作，原会话保留")
    if actual_phone:
        if normalize_account_phone(actual_phone) != normalize_account_phone(phone):
            raise RuntimeError("当前会话与所选手机号不一致，已停止操作，原会话保留")
    elif profile.get("user_id") is None:
        raise RuntimeError("无法确认当前会话的账号身份，请检查所选账号")


def split_chat_addresses(value: str) -> list[str]:
    """Split one group's address field into one or more Telegram chat addresses."""
    if not isinstance(value, str):
        return []
    addresses = []
    seen = set()
    for item in re.split(r"[|;；\r\n]+", value):
        address = item.strip()
        if address and address.casefold() not in seen:
            addresses.append(address)
            seen.add(address.casefold())
    return addresses


def resolve_private_chat(client, chat_id: int):
    """Locate a private group by numeric Chat ID using the task account's session."""
    from telethon import utils

    try:
        peer = client.get_input_entity(chat_id)
    except ValueError:
        peer = None
        for dialog in client.iter_dialogs():
            if dialog.id == chat_id:
                peer = dialog.input_entity
                break
        if peer is None:
            raise ValueError("绑定账号无法定位该私密群，请核对 Chat ID 和入群情况")
    if utils.get_peer_id(peer) != chat_id:
        raise ValueError("解析到的群 ID 与配置不一致")
    return peer


def runtime_log_path(log_root: Path, target_day: date, group_name: str = "系统") -> Path:
    return Path(log_root) / safe_folder_name(group_name) / f"{target_day.isoformat()}.log"


def clear_old_runtime_logs(log_root: Path, target_day: date, group_name: str = "系统") -> Path:
    """Keep only today's log and return the active (empty or existing) file."""
    root = Path(log_root) / safe_folder_name(group_name)
    root.mkdir(parents=True, exist_ok=True)
    active = root / f"{target_day.isoformat()}.log"
    log_base = Path(log_root).resolve()
    for path in list(log_base.glob("*/*.log")) + list(log_base.glob("*.log")):
        if (re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.stem)
                and path.stem < target_day.isoformat()
                and path.resolve().is_relative_to(log_base)):
            try:
                path.unlink()
            except OSError:
                pass
    active.touch(exist_ok=True)
    return active


def runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def validate_group_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("自定义群名称不能为空")
    if len(name) > 100:
        raise ValueError("自定义群名称不能超过 100 个字符")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((" ", ".")):
        raise ValueError('群名称不能包含 \\ / : * ? " < > |，也不能以空格或句点结尾')
    if name.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise ValueError("该群名称是 Windows 保留名称，请更换")
    return name


def group_directory(program_root: Path) -> Path:
    path = Path(program_root) / GROUP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def group_notes_path(program_root: Path, group_name: str) -> Path:
    return group_directory(program_root) / f"{validate_group_name(group_name)}.json"


def _legacy_group_notes_path(program_root: Path, group_name: str) -> Path:
    return group_directory(program_root) / f"{validate_group_name(group_name)}.txt"


def _clean_keyword_entries(values) -> list[str]:
    entries = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("备注 JSON 的 keywords 必须全部是文字")
        entry = normalized(value)
        if entry and entry not in seen:
            entries.append(entry)
            seen.add(entry)
    return entries


def write_notes_config(path: Path, keywords) -> None:
    entries = _clean_keyword_entries(keywords)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"keywords": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def migrate_group_notes_config(program_root: Path, group_name: str) -> Path:
    path = group_notes_path(program_root, group_name)
    legacy = _legacy_group_notes_path(program_root, group_name)
    if not path.exists():
        keywords = legacy.read_text(encoding="utf-8-sig").splitlines() if legacy.exists() else []
        write_notes_config(path, keywords)
    load_notes(path, allow_empty=True)
    if legacy.exists():
        legacy.unlink()
    return path


def _default_group_settings() -> dict:
    return {
        "groups": [],
        "selected_group": "",
        "output_path": str(DEFAULT_OUTPUT_PATH),
    }


def _write_group_settings(program_root: Path, settings: dict) -> None:
    path = group_directory(program_root) / GROUP_SETTINGS_NAME
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_group_settings(program_root: Path, *, read_only: bool = False) -> dict:
    group_root = Path(program_root) / GROUP_DIR_NAME
    path = group_root / GROUP_SETTINGS_NAME
    if not path.is_file():
        settings = _default_group_settings()
        if not read_only:
            _write_group_settings(program_root, settings)
        return settings
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list):
        raise ValueError("群配置文件格式无效")
    settings = _default_group_settings()
    groups = []
    for item in payload["groups"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("address"), str):
            raise ValueError("群配置文件包含无效项目")
        start_time, end_time = validate_time_range(
            item.get("start_time", "00:00"),
            item.get("end_time", "23:59"),
        )
        group = {
            "name": validate_group_name(item["name"]),
            "address": item["address"].strip(),
            "start_time": start_time,
            "end_time": end_time,
        }
        chat_id = item.get("chat_id")
        bound_account_id = item.get("bound_account_id")
        if chat_id is not None or bound_account_id is not None:
            if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id >= 0:
                raise ValueError("群配置文件包含无效的私密群 Chat ID")
            if not isinstance(bound_account_id, str) or not bound_account_id.strip():
                raise ValueError("群配置文件包含无效的绑定账号")
            bound_account_id = bound_account_id.strip()
            if bound_account_id != "legacy" and not re.fullmatch(r"[0-9a-f]{32}", bound_account_id):
                raise ValueError("群配置文件包含无效的绑定账号")
            group["chat_id"] = chat_id
            group["bound_account_id"] = bound_account_id
        groups.append(group)
    settings["groups"] = groups
    if isinstance(payload.get("selected_group"), str):
        settings["selected_group"] = payload["selected_group"]
    if isinstance(payload.get("output_path"), str) and payload["output_path"].strip():
        settings["output_path"] = payload["output_path"]
    if not read_only:
        for item in groups:
            migrate_group_notes_config(program_root, item["name"])
    return settings


def save_group_profile(
    program_root: Path,
    settings: dict,
    old_name: str,
    new_name: str,
    address: str,
    start_time: str = "00:00",
    end_time: str = "23:59",
    *,
    chat_id: int | None = None,
    bound_account_id: str | None = None,
) -> None:
    new_name = validate_group_name(new_name)
    address = address.strip()
    if chat_id is None:
        if not address:
            raise ValueError("群地址不能为空，请填写 @用户名、t.me 链接或完整群名")
        bound_account_id = None
    else:
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id >= 0:
            raise ValueError("私密群 Chat ID 必须是负数整数，例如 -1004401898428")
        if not isinstance(bound_account_id, str) or not bound_account_id.strip():
            raise ValueError("私密群必须选择绑定账号")
        bound_account_id = bound_account_id.strip()
        if bound_account_id != "legacy" and not re.fullmatch(r"[0-9a-f]{32}", bound_account_id):
            raise ValueError("绑定账号标识无效")
        address = ""
    start_time, end_time = validate_time_range(start_time, end_time)
    groups = settings.setdefault("groups", [])
    duplicate = next((item for item in groups if item["name"].casefold() == new_name.casefold() and item["name"] != old_name), None)
    if duplicate:
        raise ValueError("这个自定义群名称已经存在")

    current = next((item for item in groups if item["name"] == old_name), None) if old_name else None
    if old_name and current is None:
        raise ValueError("要修改的群配置不存在")
    if current:
        if old_name != new_name:
            old_path = group_notes_path(program_root, old_name)
            new_path = group_notes_path(program_root, new_name)
            if new_path.exists():
                raise ValueError(f"备注文件已存在：{new_path.name}")
            if old_path.exists():
                os.replace(old_path, new_path)
            else:
                write_notes_config(new_path, [])
        current.update(name=new_name, address=address, start_time=start_time, end_time=end_time)
        if chat_id is None:
            current.pop("chat_id", None)
            current.pop("bound_account_id", None)
        else:
            current["chat_id"] = chat_id
            current["bound_account_id"] = bound_account_id
    else:
        group = {"name": new_name, "address": address, "start_time": start_time, "end_time": end_time}
        if chat_id is not None:
            group["chat_id"] = chat_id
            group["bound_account_id"] = bound_account_id
        groups.append(group)
        notes_path = group_notes_path(program_root, new_name)
        if not notes_path.exists():
            write_notes_config(notes_path, [])
    if settings.get("selected_group") == old_name or not settings.get("selected_group"):
        settings["selected_group"] = new_name
    _write_group_settings(program_root, settings)


def delete_group_profile(program_root: Path, settings: dict, name: str) -> None:
    groups = settings.setdefault("groups", [])
    settings["groups"] = [item for item in groups if item.get("name") != name]
    if len(settings["groups"]) == len(groups):
        raise ValueError("要删除的群配置不存在")
    if settings.get("selected_group") == name:
        settings["selected_group"] = settings["groups"][0]["name"] if settings["groups"] else ""
    _write_group_settings(program_root, settings)


def safe_folder_name(text: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .")
    return name[:100] or "未命名"


def dated_group_output(output: Path, target_day: date, group_name: str) -> Path:
    return Path(output) / f"{target_day.month}.{target_day.day}-{safe_folder_name(group_name)}"


def today_result_exists(output: Path, target_day: date, group_name: str) -> bool:
    return dated_group_output(output, target_day, group_name).is_dir()


def capture_status_path(status_root: Path, group_name: str) -> Path:
    return Path(status_root) / f"{safe_folder_name(group_name)}抓取状态.json"


def load_capture_status(path: Path) -> dict | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("group_name"), str)
        or not isinstance(payload.get("date"), str)
        or not isinstance(payload.get("remarks"), dict)
        or any(not isinstance(item, dict) for item in payload["remarks"].values())
    ):
        raise ValueError("抓取状态 JSON 格式无效")
    return payload


def save_capture_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def notes_for_retry(notes: dict[str, str], status: dict | None) -> dict[str, str]:
    completed = {
        label
        for label, item in (status or {}).get("remarks", {}).items()
        if item.get("status") == "已完成"
    }
    return {keyword: label for keyword, label in notes.items() if label not in completed}


def _notes_from_entries(entries) -> dict[str, str]:
    notes: dict[str, str] = {}
    for line in entries:
        aliases = [normalized(part) for part in line.split("/") if part.strip()]
        if aliases:
            canonical = aliases[0]
            for alias in aliases:
                notes.setdefault(alias, canonical)
    return notes


def load_notes(path: Path, allow_empty: bool = False) -> dict[str, str]:
    if path.suffix.casefold() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or not isinstance(payload.get("keywords"), list):
            raise ValueError("备注 JSON 必须包含 keywords 数组")
        entries = _clean_keyword_entries(payload["keywords"])
    else:
        entries = path.read_text(encoding="utf-8-sig").splitlines()
    notes = _notes_from_entries(entries)
    if not notes and not allow_empty:
        raise ValueError("备注名单是空的")
    return notes


def load_group_notes(path: Path) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Load the group's default notes and the optional per-link exclusive notes."""
    default_notes = load_notes(path, allow_empty=True)
    address_notes: dict[str, dict[str, str]] = {}
    if path.suffix.casefold() != ".json":
        return default_notes, address_notes
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    links = payload.get("links") if isinstance(payload, dict) else None
    if links is None:
        return default_notes, address_notes
    if not isinstance(links, dict):
        raise ValueError("备注 JSON 的 links 必须是 {链接: 备注数组}")
    for address, entries in links.items():
        if not isinstance(address, str) or not address.strip() or not isinstance(entries, list):
            raise ValueError("备注 JSON 的 links 必须是 {链接: 备注数组}")
        notes = _notes_from_entries(_clean_keyword_entries(entries))
        if not notes:
            raise ValueError(f"备注 JSON 的 links「{address.strip()}」是空的")
        address_notes[address.strip()] = notes
    return default_notes, address_notes


def parse_day(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def parse_clock(value: str) -> clock_time:
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as exc:
        raise ValueError("时间格式应为 HH:MM，例如 14:30") from exc


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_processing_status(
    current: int, total: int, elapsed: float, *, status: str | None = None, total_elapsed: float | None = None,
) -> str:
    heading = status if status is not None else f"正在处理：{current} / {total}"
    duration = elapsed if total_elapsed is None else total_elapsed
    estimate_label = "本阶段预计剩余" if status is not None else "预计剩余"
    prefix = f"{heading}｜已耗时 {format_duration(duration)}｜{estimate_label}"
    if current <= 0 or total <= 0:
        return f"{prefix}：计算中…"
    remaining = elapsed * max(0, total - current) / current
    return f"{prefix} {format_duration(remaining)}"


def validate_time_range(start_time: str, end_time: str) -> tuple[str, str]:
    start, end = parse_clock(start_time), parse_clock(end_time)
    if start > end:
        raise ValueError("开始时间不能晚于结束时间")
    return start.strftime("%H:%M"), end.strftime("%H:%M")


def caption_matches_keyword(caption: str, keyword: str, exact: bool = False) -> bool:
    compact = lambda text: "".join(char for char in text if char.isalnum() or "\u4e00" <= char <= "\u9fff")
    if exact:
        return compact(caption) == compact(keyword.casefold())
    return keyword.casefold() in caption


def build_selection(
    messages,
    notes: dict[str, str],
    bridge_excluded_labels: set[str] | None = None,
    exact_labels: set[str] | None = None,
    log=None,
) -> dict[int, set[str]]:
    messages = list(messages)
    album_labels: dict[int, set[str]] = defaultdict(set)
    selection: dict[int, set[str]] = defaultdict(set)

    for message in messages:
        caption = normalized(message.raw_text or "").casefold()
        for keyword, label in notes.items():
            if not caption_matches_keyword(caption, keyword, bool(exact_labels and label in exact_labels)):
                continue
            selection[message.id].add(label)
            if log:
                log(f"【识别】{media_group_description([message])}：备注关键词「{keyword}」命中 → {label}")
            if message.grouped_id:
                album_labels[message.grouped_id].add(label)

    albums: dict[int, list] = defaultdict(list)
    for message in messages:
        if message.grouped_id:
            albums[message.grouped_id].append(message)
    ordered_albums = sorted(
        albums,
        key=lambda group_id: min(message.id for message in albums[group_id]),
    )
    positions = {group_id: index for index, group_id in enumerate(ordered_albums)}
    for label in notes.values():
        if bridge_excluded_labels and label in bridge_excluded_labels:
            continue
        anchors = [group_id for group_id in ordered_albums if label in album_labels.get(group_id, set())]
        for left_id, right_id in zip(anchors, anchors[1:]):
            left, right = positions[left_id], positions[right_id]
            between = ordered_albums[left + 1 : right]
            if (
                between
                and all(not album_labels.get(group_id) for group_id in between)
            ):
                for group_id in between:
                    album_labels[group_id].add(label)
                    if log:
                        log(f"【识别】{media_group_description(albums[group_id])}：位于同名相册 {left_id}～{right_id} 之间 → {label}")

    if log:
        for group_id, labels in album_labels.items():
            log(f"【识别】{media_group_description(albums[group_id])}：整组 {len(albums[group_id])} 张 → {'、'.join(sorted(labels))}")

    for message in messages:
        if message.grouped_id in album_labels:
            selection[message.id].update(album_labels[message.grouped_id])
    return dict(selection)


def ordered_media_groups(messages) -> list[list]:
    groups: dict[tuple[str, int], list] = defaultdict(list)
    for message in messages:
        key = ("album", message.grouped_id) if message.grouped_id else ("message", message.id)
        groups[key].append(message)
    return sorted(groups.values(), key=lambda group: min(message.id for message in group))


def add_similar_immediate_groups(
    messages,
    selection: dict[int, set[str]],
    labels: set[str],
    group_similarity,
    bidirectional_labels: set[str] | None = None,
    log=None,
) -> dict[int, set[str]]:
    selection = defaultdict(set, {message_id: set(value) for message_id, value in selection.items()})
    groups = ordered_media_groups(messages)
    bidirectional_labels = bidirectional_labels or set()
    anchors = []
    for index, group in enumerate(groups):
        for label in labels:
            if not any(label in selection.get(message.id, set()) for message in group):
                continue
            steps = (1, -1) if label in bidirectional_labels else (1,)
            anchors.extend((index, step, label) for step in steps)
    for index, step, label in anchors:
        previous = groups[index]
        for distance in range(1, VISUAL_ADJACENT_EXTENSION_GROUPS + 1):
            neighbor_index = index + step * distance
            if not 0 <= neighbor_index < len(groups):
                break
            candidate = groups[neighbor_index]
            similar = group_similarity(previous, candidate)
            if log:
                log(f"【识别】{label} 相邻组检查：基准消息 {previous[0].id}；"
                    f"{media_group_description(candidate)}；"
                    + (f"相似，整组 {len(candidate)} 张归入 {label}" if similar else "未通过相似度检查，不据此归类"))
            if not similar:
                break
            for message in candidate:
                selection[message.id].add(label)
            previous = candidate
    return dict(selection)


def collect_adjacent_preview_messages(
    messages,
    selection: dict[int, set[str]],
    labels: set[str],
    bidirectional_labels: set[str] | None = None,
) -> set[int]:
    """Return messages needed to compare each special-label group with neighbors."""
    groups = ordered_media_groups(messages)
    bidirectional_labels = bidirectional_labels or set()
    preview_ids: set[int] = set()
    for index, group in enumerate(groups):
        matched_labels = {
            label
            for label in labels
            if any(label in selection.get(message.id, set()) for message in group)
        }
        if not matched_labels:
            continue
        preview_ids.update(message.id for message in group)
        neighbor_indexes: set[int] = {index + 1, index + 2}
        if matched_labels.intersection(bidirectional_labels):
            neighbor_indexes.update({index - 1, index - 2})
        for neighbor_index in neighbor_indexes:
            if 0 <= neighbor_index < len(groups):
                preview_ids.update(message.id for message in groups[neighbor_index])
    return preview_ids


def collect_label_group_messages(
    messages,
    selection: dict[int, set[str]],
    labels: set[str],
) -> set[int]:
    """Return every message of each group that matched one of the labels."""
    preview_ids: set[int] = set()
    for group in ordered_media_groups(messages):
        if any(label in selection.get(message.id, set()) for message in group for label in labels):
            preview_ids.update(message.id for message in group)
    return preview_ids


class OcrError(RuntimeError):
    """Required OCR did not complete reliably."""


OCR_DEVICE = "gpu:0"
OCR_MODEL_DET = "PP-OCRv6_small_det"
OCR_MODEL_REC = "PP-OCRv6_small_rec"


def ocr_model_root() -> Path:
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base.parent / "models"
_OCR_ENGINE = None
_OCR_ENGINE_ERROR: Exception | None = None


def get_ocr_engine():
    global _OCR_ENGINE, _OCR_ENGINE_ERROR
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    if _OCR_ENGINE_ERROR is not None:
        raise OcrError(
            "GPU OCR 不可用，CPU 识别已禁用。请修复环境后重启软件。"
            "原因：" + ocr_engine_error_text()
        ) from _OCR_ENGINE_ERROR
    try:
        import paddle

        if not paddle.is_compiled_with_cuda():
            raise RuntimeError("当前 Paddle 不支持 CUDA，请安装 paddlepaddle-gpu")
        if paddle.device.cuda.device_count() < 1:
            raise RuntimeError("未检测到可用的 CUDA GPU，请检查显卡和驱动")
        paddle.set_device(OCR_DEVICE)
        if str(paddle.device.get_device()).lower() != OCR_DEVICE:
            raise RuntimeError(f"Paddle 未切换到 {OCR_DEVICE}，实际设备为 {paddle.device.get_device()}")

        from paddleocr import PaddleOCR

        engine = PaddleOCR(
            text_detection_model_name=OCR_MODEL_DET,
            text_recognition_model_name=OCR_MODEL_REC,
            device=OCR_DEVICE,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            enable_hpi=False,
            use_tensorrt=False,
            precision="fp32",
            enable_mkldnn=False,
        )
    except Exception as exc:  # pragma: no cover - depends on optional GPU runtime/model files
        _OCR_ENGINE_ERROR = exc
        raise OcrError(
            "GPU OCR 初始化失败，CPU 识别已禁用。请修复环境后重启软件。"
            "原因：" + ocr_engine_error_text()
        ) from exc
    _OCR_ENGINE = engine
    return _OCR_ENGINE


def ocr_engine_error_text() -> str:
    if _OCR_ENGINE_ERROR is None:
        return "未知错误"
    details: list[str] = []
    error: BaseException | None = _OCR_ENGINE_ERROR
    while error is not None and len(details) < 3:
        detail = " ".join(str(error).split())
        details.append(f"{type(error).__name__}: {detail[:300]}")
        error = error.__cause__ or error.__context__
    return "；".join(details)


def compact_text(text: str) -> str:
    return "".join(char for char in normalized(text).casefold() if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def ocr_labels_from_payload(payload: bytes, labels: set[str], ocr_engine=None, on_error=None, cleanup=None) -> set[str]:
    """Read image text and return configured labels found in any OCR text line."""
    if not labels:
        return set()
    if not payload:
        raise OcrError("OCR 必需图片为空，无法完成识别；本次任务已中止")
    engine = get_ocr_engine() if ocr_engine is None else ocr_engine
    try:
        import numpy as np

        with Image.open(io.BytesIO(payload)) as image:
            image_array = np.asarray(image.convert("RGB"))
        results = list(engine.predict(image_array))
        if not results:
            raise RuntimeError("OCR 没有返回该图片的结果对象")
    except Exception as exc:
        if on_error:
            on_error(type(exc).__name__)
        if isinstance(exc, OcrError):
            raise
        raise OcrError(
            "OCR 识别未完成，本次任务已中止；不会使用 CPU 或把失败当作未命中。"
            f"原因：{type(exc).__name__}: {exc}"
        ) from exc
    try:
        recognized: list[str] = []
        for result in results:
            if not isinstance(result, dict) or "rec_texts" not in result:
                raise RuntimeError("OCR 返回的结果结构无效")
            texts = result["rec_texts"]
            if not isinstance(texts, (list, tuple)):
                raise RuntimeError("OCR 返回的 rec_texts 结构无效")
            recognized.extend(str(text) for text in texts)
    except Exception as exc:
        if on_error:
            on_error(type(exc).__name__)
        raise OcrError(
            "OCR 结果读取未完成，本次任务已中止；不会使用 CPU 或把失败当作未命中。"
            f"原因：{type(exc).__name__}: {exc}"
        ) from exc
    clean = cleanup or (lambda text: "".join(normalized(text).casefold().split()))
    haystack = clean("".join(recognized))
    variants = {
        "战狼": ("战狼", "戰狼"),
        "红人馆": ("红人馆", "紅人館"),
        "香奈儿": ("香奈儿", "香奈兒"),
        # Tianji adjacent matching deliberately uses only this short marker.
        "天机阁特围": ("天机阁", "天機閣", "天机"),
        "天机阁杀料": ("天机阁", "天機閣", "天机"),
    }
    matched = set()
    for label in labels:
        candidates = variants.get(label, (label,))
        if any(clean(candidate) in haystack for candidate in candidates):
            matched.add(label)
    return matched


def _first_media_message(group) -> object:
    """Return the earliest message in a media group (the album's first image)."""
    return min(group, key=lambda message: getattr(message, "id", 0))


def collect_adjacent_first_preview_messages(
    messages,
    selection: dict[int, set[str]],
    labels: set[str],
    bidirectional_labels: set[str] | None = None,
    checked_pairs: set[tuple[int, int]] | None = None,
) -> set[int]:
    """Return only the first image ID from each requested adjacent group."""
    messages = list(messages)
    groups = ordered_media_groups(messages)
    bidirectional_labels = bidirectional_labels or set()
    preview_ids: set[int] = set()
    for index, group in enumerate(groups):
        matched_labels = {
            label
            for label in labels
            if any(label in selection.get(message.id, set()) for message in group)
        }
        for label in matched_labels:
            neighbor_indexes = {index + 1}
            if label in bidirectional_labels:
                neighbor_indexes.add(index - 1)
            for neighbor_index in neighbor_indexes:
                if 0 <= neighbor_index < len(groups):
                    candidate = groups[neighbor_index]
                    if any(selection.get(message.id, set()).intersection(labels) for message in candidate):
                        continue
                    if checked_pairs and (_first_media_message(group).id, _first_media_message(candidate).id) in checked_pairs:
                        continue
                    preview_ids.add(_first_media_message(candidate).id)
    return preview_ids


def add_first_image_ocr_immediate_groups(
    messages,
    selection: dict[int, set[str]],
    labels: set[str],
    payloads: dict[int, bytes],
    *,
    ocr_engine=None,
    bidirectional_labels: set[str] | None = None,
    detected_labels: set[str] | None = None,
    checked_pairs: set[tuple[int, int]] | None = None,
    ocr_cache: dict | None = None,
    group_similarity=None,
    log=None,
    on_progress=None,
) -> dict[int, set[str]]:
    """Use OCR on each adjacent group's first image, then classify that whole group."""
    messages = list(messages)
    selection = defaultdict(set, {message_id: set(value) for message_id, value in selection.items()})
    if not labels:
        return dict(selection)
    groups = ordered_media_groups(messages)
    bidirectional_labels = bidirectional_labels or set()
    requests: dict[int, set[str]] = defaultdict(set)
    candidate_groups: dict[int, list] = {}
    candidate_anchors: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for index, group in enumerate(groups):
        matched_labels = {
            label
            for label in labels
            if any(label in selection.get(message.id, set()) for message in group)
        }
        for label in matched_labels:
            neighbor_indexes = {index + 1}
            if label in bidirectional_labels:
                neighbor_indexes.add(index - 1)
            for neighbor_index in neighbor_indexes:
                if 0 <= neighbor_index < len(groups):
                    candidate = groups[neighbor_index]
                    if any(selection.get(message.id, set()).intersection(labels) for message in candidate):
                        continue
                    first = _first_media_message(candidate)
                    if checked_pairs and (_first_media_message(group).id, first.id) in checked_pairs:
                        continue
                    requests[first.id].add(label)
                    candidate_groups[first.id] = candidate
                    candidate_anchors[first.id][label].append(group)

    total = len(requests)
    messages_by_id = {message.id: message for message in messages}
    for done, first_id in enumerate(sorted(requests), 1):
        requested_labels = requests[first_id]
        payload = payloads.get(first_id)
        first = messages_by_id[first_id]
        if not payload:
            raise OcrError(f"相邻组首图缺失：消息 {first_id}；无法完成 OCR，本次任务已中止")
        target_labels = detected_labels if detected_labels is not None else requested_labels
        cache_key = (first_id, frozenset(target_labels))
        if ocr_cache is not None and cache_key in ocr_cache:
            hits = ocr_cache[cache_key]
        else:
            hits = ocr_labels_from_payload(
                payload, target_labels, ocr_engine,
                on_error=lambda error, m=first: log and log(
                    f"【识别】OCR失败：{media_group_description([m])}（{error}）"
                ),
            )
            if ocr_cache is not None:
                ocr_cache[cache_key] = hits
        candidate = candidate_groups[first_id]
        labels_to_add = hits if detected_labels is not None else requested_labels.intersection(hits)
        if detected_labels is not None:
            if log:
                log(
                    f"【识别】相邻组首图 OCR：{media_group_description(candidate)}；"
                    + (f"命中 {'、'.join(sorted(labels_to_add))}，整组 {len(candidate)} 张归类"
                       if labels_to_add else "未命中指定关键词，整组跳过")
                )
        else:
            for label in requested_labels:
                if log:
                    log(
                        f"【识别】{label} 相邻组首图 OCR：{media_group_description(candidate)}；"
                        + (f"命中天机阁/天機閣/天机，整组 {len(candidate)} 张归入 {label}"
                           if label in labels_to_add else "首图未命中天机阁/天機閣/天机")
                    )
        for label in requested_labels - labels_to_add:
            if group_similarity:
                similar = any(group_similarity(anchor, candidate)
                              for anchor in candidate_anchors[first_id][label])
                if similar:
                    labels_to_add.add(label)
                if log:
                    log(f"【识别】{label} 相邻组相似度补抓：{media_group_description(candidate)}；"
                        + (f"相似，整组 {len(candidate)} 张归入 {label}" if similar
                           else "首图和相似度均未命中，不从此组继续扩展"))
        if checked_pairs is not None:
            checked_pairs.update((_first_media_message(anchor).id, first_id)
                                 for anchors in candidate_anchors[first_id].values() for anchor in anchors)
        for label in labels_to_add:
            for message in candidate:
                selection[message.id].add(label)
        if on_progress:
            on_progress(done, total)
    return dict(selection)


def filter_unwanted_ocr_groups(
    messages,
    selection: dict[int, set[str]],
    labels: set[str],
    payloads: dict[int, bytes],
    markers: set[str],
    ocr_engine=None,
    log=None,
    on_progress=None,
) -> tuple[dict[int, set[str]], dict[str, set[int]]]:
    """Drop whole caption-matched groups whose images contain any unwanted OCR marker."""
    messages = list(messages)
    selection = defaultdict(set, {message_id: set(value) for message_id, value in selection.items()})
    ignored: dict[str, set[int]] = defaultdict(set)
    if not labels or not markers:
        return dict(selection), {}
    candidates = [
        group
        for group in ordered_media_groups(messages)
        if any(label in selection.get(message.id, set()) for message in group for label in labels)
    ]
    for done, group in enumerate(candidates, 1):
        matched_labels = {
            label
            for label in labels
            if any(label in selection.get(message.id, set()) for message in group)
        }
        hit_markers: set[str] = set()
        for message in sorted(group, key=lambda item: item.id):
            payload = payloads.get(message.id)
            if not payload:
                raise OcrError(f"过滤所需图片缺失：消息 {message.id}；无法完成 OCR，本次任务已中止")
            hits = ocr_labels_from_payload(
                payload,
                markers,
                ocr_engine,
                on_error=lambda error, current=message: log and log(
                    f"【识别】OCR失败：消息 {current.id}（{error}）"
                ),
                cleanup=compact_text,
            )
            if hits:
                hit_markers.update(hits)
                break
        if hit_markers:
            for message in group:
                remaining = selection.get(message.id, set()) - matched_labels
                if remaining:
                    selection[message.id] = remaining
                else:
                    selection.pop(message.id, None)
            for label in matched_labels:
                ignored[label].update(message.id for message in group)
            if log:
                log(f"【识别】{'、'.join(sorted(matched_labels))}：{media_group_description(group)} "
                    f"命中「{'、'.join(sorted(hit_markers))}」→ 整组 {len(group)} 张忽略")
        elif log:
            log(f"【识别】{'、'.join(sorted(matched_labels))}：{media_group_description(group)} "
                f"未命中不要组特征词 → 保留整组 {len(group)} 张")
        if on_progress:
            on_progress(done, len(candidates))
    return dict(selection), dict(ignored)


def image_color_signature(payload: bytes) -> tuple[tuple[float, ...], float] | None:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image = image.convert("RGB")
            aspect = image.width / max(image.height, 1)
            image.thumbnail((128, 128))
            histogram = image.histogram()
    except (OSError, UnidentifiedImageError):
        return None
    total = sum(histogram) or 1
    return tuple(value / total for value in histogram), aspect


def image_groups_are_similar(
    anchor_payloads: list[bytes], candidate_payloads: list[bytes],
    *, min_intersection: float = 0.60, max_aspect_ratio: float = 1.35,
) -> bool:
    anchors = [signature for payload in anchor_payloads if (signature := image_color_signature(payload))]
    candidates = [signature for payload in candidate_payloads if (signature := image_color_signature(payload))]
    if not anchors or not candidates:
        return False

    def similar(candidate) -> bool:
        histogram, aspect = candidate
        for anchor_histogram, anchor_aspect in anchors:
            aspect_ratio = max(aspect, anchor_aspect) / max(min(aspect, anchor_aspect), 0.01)
            intersection = sum(min(left, right) for left, right in zip(histogram, anchor_histogram))
            if aspect_ratio <= max_aspect_ratio and intersection >= min_intersection:
                return True
        return False

    return any(similar(candidate) for candidate in candidates)


def build_download_selection(
    messages,
    notes: dict[str, str],
    special_adjacent_labels: set[str] | None = None,
    group_similarity=None,
    bidirectional_adjacent_labels: set[str] | None = None,
    exact_labels: set[str] | None = None,
    log=None,
    similarity_labels: set[str] | None = None,
) -> dict[int, set[str]]:
    messages = list(messages)
    if not notes:
        return {message.id: {"全部图片"} for message in messages}
    selection = build_selection(
        messages,
        notes,
        bridge_excluded_labels=special_adjacent_labels,
        exact_labels=exact_labels,
        log=log,
    )
    labels_for_similarity = special_adjacent_labels if similarity_labels is None else similarity_labels
    if labels_for_similarity and group_similarity:
        selection = add_similar_immediate_groups(
            messages,
            selection,
            labels_for_similarity,
            group_similarity,
            bidirectional_labels=bidirectional_adjacent_labels,
            log=log,
        )
    return selection


def build_note_message_ids(
    messages, notes: dict[str, str], exact_labels: set[str] | None = None,
) -> dict[str, set[int]]:
    matches: dict[str, set[int]] = defaultdict(set)
    for message in messages:
        caption = normalized(message.raw_text or "").casefold()
        for keyword, label in notes.items():
            if caption_matches_keyword(caption, keyword, bool(exact_labels and label in exact_labels)):
                matches[label].add(message.id)
    return dict(matches)


class TelegramDownloaderApp:
    def __init__(
        self,
        root: Tk,
        app_data: Path = APP_DATA,
        show_account_dialog: bool = True,
        program_root: Path | None = None,
        status_root: Path | None = None,
        workflow_log: Path | None = None,
    ):
        self.root = root
        self.app_data = app_data
        self.program_root = Path(program_root) if program_root else runtime_root()
        self.status_root = Path(status_root) if status_root else STATUS_OUTPUT_DIR
        self._workflow_log_path = Path(workflow_log).expanduser() if workflow_log else None
        try:
            self.settings = load_group_settings(self.program_root)
        except (OSError, ValueError, json.JSONDecodeError):
            self.settings = _default_group_settings()
        self.root.title(f"登录飞机提取图片 {APP_VERSION}")
        self.window_preset = self._initial_window_preset()
        window_width, window_height = self._window_size_for_preset(self.window_preset)
        window_x = max((self.root.winfo_screenwidth() - window_width) // 2, 0)
        window_y = max((self.root.winfo_screenheight() - window_height) // 2, 0)
        self.root.geometry(f"{window_width}x{window_height}+{window_x}+{window_y}")
        self.root.minsize(1060, 720)
        self.root.configure(background=DARK_BG)
        self.logged_in = False
        self.active_account_id = ""
        self.active_user_id = None
        self.selected_account_id = ""
        self.adding_account = False
        self._busy = False
        self._busy_widget_states = {}
        self.account_profiles = []
        self.busy_widgets = []
        self.log_root = self.program_root / RUNTIME_LOG_DIR_NAME
        self._log_lock = threading.RLock()
        self._log_day: date | None = None
        self._log_group = "系统"
        self._log_rollover_id = None
        self._ui_queue: queue.Queue = queue.Queue()
        self._ui_pump_id = None
        self._login_token = ""
        self._ui_heartbeat = time.monotonic()
        self._watchdog_stop = threading.Event()

        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self._configure_styles()
        self.root.option_add("*TCombobox*Listbox.font", "{Microsoft YaHei UI} 13")

        self.api_id = StringVar(master=root)
        self.api_hash = StringVar(master=root)
        self.phone = StringVar(master=root)
        self.account_choice = StringVar(master=root)
        self.account_name = StringVar(master=root)
        self.chat = StringVar(master=root, value=self.settings.get("selected_group", ""))
        self.day = StringVar(master=root, value=datetime.now(CN_TZ).strftime("%Y-%m-%d"))
        self.time_range = StringVar(master=root, value="00:00 ～ 23:59")
        self.notes_path = StringVar(master=root)
        self.output_path = StringVar(master=root, value=self.settings["output_path"])
        self.account_status = StringVar(master=root, value="未登录，请选择账号")
        self.task_status = StringVar(master=root, value="等待操作")
        self.first_capture_status = StringVar(master=root, value="今日未首抓")
        self.progress_summary = StringVar(master=root, value="")
        self._capture_started = None
        self._capture_timer = None
        self._capture_generation = 0
        self._workflow_mode = False
        self._workflow_group = ""
        self._workflow_exit_code: int | None = None
        self.root.bind("<Destroy>", self._on_capture_window_destroyed, add="+")

        self._build_ui()
        self.refresh_account_profiles()
        self._load_runtime_log()
        self._schedule_log_rollover()
        self._pump_ui_callbacks()
        self.root.after_idle(lambda: self._enable_dark_title_bar(self.root))
        self.reload_group_profiles(self.chat.get())
        self._start_freeze_watchdog()
        if show_account_dialog:
            # Idle callbacks can run while the main window still has a 1x1 geometry.
            self._startup_account_pending = True
            self.root.bind("<Map>", self._on_main_window_mapped, add="+")
            if self.root.winfo_ismapped():
                self._on_main_window_mapped()

    def _on_main_window_mapped(self, event=None) -> None:
        if event is not None and event.widget is not self.root:
            return
        if self._startup_account_pending:
            self._startup_account_pending = False
            self.root.after_idle(self._restore_selected_account)

    def _restore_selected_account(self) -> None:
        if self._busy or self.selected_account_id or self.adding_account:
            return
        try:
            account_id = load_selected_account(self.app_data)
        except (OSError, ValueError) as exc:
            self.log(f"【账号】默认账号记录读取失败（{type(exc).__name__}），未切换账号、未覆盖文件")
            messagebox.showwarning("默认账号不可用", "无法读取本地默认账号，请手动选择。原文件和会话已保留。")
            self.open_login_settings()
            return
        if account_id:
            self.select_account(account_id, remember=False)
            if self.selected_account_id == account_id:
                self.login(automatic=True)
                return
        self.open_login_settings()

    def _configure_styles(self) -> None:
        style = self.style
        style.configure(".", font=("Microsoft YaHei UI", 12))
        style.configure("TFrame", background=DARK_BG)
        style.configure("Dark.TFrame", background=DARK_BG)
        style.configure("Card.TFrame", background=DARK_SURFACE, relief="flat")
        style.configure("TLabel", background=DARK_BG, foreground=DARK_TEXT, font=("Microsoft YaHei UI", 13))
        style.configure("Card.TLabel", background=DARK_SURFACE, foreground=DARK_TEXT, font=("Microsoft YaHei UI", 13))
        style.configure("Title.TLabel", background=DARK_BG, foreground=DARK_TEXT, font=("Microsoft YaHei UI", 24, "bold"))
        style.configure("Section.TLabel", background=DARK_BG, foreground=DARK_TEXT, font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Hint.TLabel", background=DARK_BG, foreground=DARK_MUTED, font=("Microsoft YaHei UI", 12))
        style.configure("CardHint.TLabel", background=DARK_SURFACE, foreground=DARK_MUTED, font=("Microsoft YaHei UI", 12))
        style.configure("Online.TLabel", background=DARK_BG, foreground=DARK_SUCCESS, font=("Segoe UI", 12, "bold"))
        style.configure("Offline.TLabel", background=DARK_BG, foreground=DARK_MUTED, font=("Segoe UI", 12, "bold"))
        style.configure("Status.TLabel", background=DARK_SURFACE, foreground=DARK_TEXT, font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("FirstCapturePending.TLabel", background=DARK_SURFACE, foreground=DARK_ACCENT, font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("FirstCaptureDone.TLabel", background=DARK_SURFACE, foreground=DARK_SUCCESS, font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Brand.TLabel", background=DARK_BG, foreground=DARK_TEXT, font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("BrandSub.TLabel", background=DARK_BG, foreground="#D6DAE0", font=("Microsoft YaHei UI", 13))
        style.configure("Workspace.TLabel", background=DARK_BG, foreground=DARK_ACCENT, font=("Microsoft YaHei UI", 11))
        style.configure("PanelNo.TLabel", background=DARK_SURFACE, foreground=DARK_ACCENT, font=("Segoe UI", 20, "bold"))
        style.configure("PanelTitle.TLabel", background=DARK_SURFACE, foreground=DARK_TEXT, font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("PanelSection.TLabel", background=DARK_SURFACE, foreground=DARK_MUTED, font=("Microsoft YaHei UI", 11))
        style.configure("PanelField.TLabel", background=DARK_SURFACE, foreground=DARK_MUTED, font=("Microsoft YaHei UI", 12))
        style.configure("BottomKey.TLabel", background=DARK_BOTTOM, foreground=DARK_DIM, font=("Microsoft YaHei UI", 11))
        style.configure("BottomValue.TLabel", background=DARK_BOTTOM, foreground="#CFD3D8", font=("Microsoft YaHei UI", 11))
        style.configure("BottomHint.TLabel", background=DARK_BOTTOM, foreground="#8A9097", font=("Microsoft YaHei UI", 12))

        style.configure(
            "TButton",
            background=DARK_FIELD,
            foreground=DARK_TEXT,
            bordercolor=DARK_BORDER,
            lightcolor=DARK_BORDER,
            darkcolor=DARK_BORDER,
            padding=(16, 9),
            relief="flat",
        )
        style.map("TButton", background=[("active", "#33373C"), ("disabled", "#232529")], foreground=[("disabled", DARK_DIM)])
        style.configure("Tool.TButton", padding=(14, 9), font=("Microsoft YaHei UI", 12))
        style.map("Tool.TButton", background=[("active", "#33373C"), ("disabled", "#232529")], foreground=[("disabled", DARK_DIM)])
        style.configure("Small.TButton", padding=(10, 7), font=("Microsoft YaHei UI", 12))
        style.map("Small.TButton", background=[("active", "#33373C"), ("disabled", "#232529")], foreground=[("disabled", DARK_DIM)])
        style.configure("Preset.TButton", padding=(16, 7), font=("Microsoft YaHei UI", 12))
        style.map("Preset.TButton", background=[("active", "#33373C"), ("disabled", "#232529")], foreground=[("disabled", DARK_DIM)])
        style.configure("PresetActive.TButton", background=DARK_ACCENT, foreground="#241A08", bordercolor=DARK_ACCENT, padding=(16, 7), font=("Microsoft YaHei UI", 12, "bold"))
        style.map("PresetActive.TButton", background=[("active", "#F0B355"), ("disabled", "#6B5527")], foreground=[("disabled", "#3A2E12")])
        style.configure("Accent.TButton", background=DARK_ACCENT, foreground="#241A08", bordercolor=DARK_ACCENT, padding=(22, 12), font=("Microsoft YaHei UI", 12, "bold"))
        style.map("Accent.TButton", background=[("active", "#F0B355"), ("pressed", "#D18F2C"), ("disabled", "#6B5527")], foreground=[("disabled", "#3A2E12")])
        style.configure("Danger.TButton", background=DARK_SURFACE, foreground=DARK_DANGER, bordercolor=DARK_BORDER, padding=(14, 9), font=("Microsoft YaHei UI", 12))
        style.map("Danger.TButton", background=[("active", "#2B2527")], foreground=[("active", "#F08A76"), ("disabled", DARK_DIM)])

        style.configure("TEntry", fieldbackground=DARK_FIELD, foreground=DARK_TEXT, insertcolor=DARK_TEXT, bordercolor=DARK_BORDER, padding=9, font=("Microsoft YaHei UI", 13))
        style.configure("Dark.TEntry", fieldbackground=DARK_FIELD, foreground=DARK_TEXT, insertcolor=DARK_TEXT, bordercolor=DARK_BORDER, padding=9, font=("Microsoft YaHei UI", 13))
        style.map("Dark.TEntry", fieldbackground=[("readonly", DARK_FIELD_RO), ("disabled", "#232529")], foreground=[("readonly", "#AEB3B9"), ("disabled", DARK_DIM)])
        style.configure("TCombobox", fieldbackground=DARK_FIELD, background=DARK_FIELD, foreground=DARK_TEXT, arrowcolor=DARK_MUTED, bordercolor=DARK_BORDER, padding=8, font=("Microsoft YaHei UI", 13))
        style.configure("Dark.TCombobox", fieldbackground=DARK_FIELD, background=DARK_FIELD, foreground=DARK_TEXT, arrowcolor=DARK_MUTED, bordercolor=DARK_BORDER, padding=8, font=("Microsoft YaHei UI", 13))
        style.map("Dark.TCombobox", fieldbackground=[("readonly", DARK_FIELD), ("disabled", "#232529")], foreground=[("readonly", DARK_TEXT), ("disabled", DARK_DIM)])
        style.configure("TCheckbutton", background=DARK_BG, foreground=DARK_TEXT, font=("Microsoft YaHei UI", 13))
        style.map("TCheckbutton", background=[("active", DARK_BG)], foreground=[("disabled", DARK_DIM)])
        style.configure("TLabelframe", background=DARK_BG, foreground=DARK_TEXT, bordercolor=DARK_BORDER)
        style.configure("TLabelframe.Label", background=DARK_BG, foreground=DARK_TEXT)
        style.configure("Treeview", background=DARK_SURFACE, fieldbackground=DARK_SURFACE, foreground=DARK_TEXT, bordercolor=DARK_BORDER, rowheight=34, font=("Microsoft YaHei UI", 12))
        style.map("Treeview", background=[("selected", DARK_ACCENT_SOFT)], foreground=[("selected", "#F0C070")])
        style.configure("Treeview.Heading", background=DARK_FIELD, foreground=DARK_TEXT, relief="flat", font=("Microsoft YaHei UI", 12, "bold"))
        style.map("Treeview.Heading", background=[("active", "#33373C")])
        style.configure("Dark.TSeparator", background=DARK_BORDER_SOFT)
        style.configure("Dark.Vertical.TScrollbar", background="#3B424C", troughcolor=DARK_FIELD, bordercolor=DARK_FIELD, arrowcolor=DARK_MUTED)
        style.configure("DarkList.Treeview", background=DARK_FIELD, fieldbackground=DARK_FIELD, foreground=DARK_TEXT,
                        bordercolor=DARK_BORDER, rowheight=27, font=("Microsoft YaHei UI", 13), relief="flat")
        style.map("DarkList.Treeview", background=[("selected", DARK_ACCENT_SOFT)], foreground=[("selected", "#F0C070")])
        style.layout("DarkList.Treeview", [
            ("Treeview.field", {"sticky": "nswe", "border": 1, "children": [
                ("Treeview.padding", {"sticky": "nswe", "children": [
                    ("Treeview.treearea", {"sticky": "nswe"}),
                ]}),
            ]}),
        ])
        style.configure("Dark.Horizontal.TProgressbar", background=DARK_ACCENT, troughcolor="#2A2D31", bordercolor=DARK_BORDER, lightcolor=DARK_ACCENT, darkcolor=DARK_ACCENT, thickness=9)

    @staticmethod
    def _enable_dark_title_bar(window) -> None:
        try:
            window.update_idletasks()
            handle = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
            enabled = ctypes.c_int(1)
            for attribute in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    handle, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
                ) == 0:
                    break
            ctypes.windll.user32.SetWindowPos(handle, 0, 0, 0, 0, 0, 0x27)
        except (AttributeError, OSError):
            pass

    def _center_dialog(self, window, width: int, height: int) -> None:
        self.root.update_idletasks()
        window.withdraw()
        window.geometry(f"{width}x{height}")
        window.update_idletasks()
        frame_x = window.winfo_rootx() - window.winfo_x()
        frame_y = window.winfo_rooty() - window.winfo_y()
        if self.root.winfo_ismapped() and self.root.winfo_width() > 1:
            x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2 - frame_x
            y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2 - frame_y
        else:
            x = max(0, (self.root.winfo_screenwidth() - width) // 2)
            y = max(0, (self.root.winfo_screenheight() - height) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.deiconify()

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, style="Dark.TFrame")
        shell.pack(fill="both", expand=True)

        header = ttk.Frame(shell, style="Dark.TFrame", padding=(22, 14, 22, 12))
        header.pack(fill="x")
        brand = ttk.Frame(header, style="Dark.TFrame")
        brand.pack(side="left")
        ttk.Label(brand, text="飞机抓图", style="Brand.TLabel").pack(side="left")
        ttk.Label(brand, text=" / 本地提取工作室", style="BrandSub.TLabel").pack(side="left")

        account = ttk.Frame(header, style="Dark.TFrame")
        account.pack(side="right")
        ttk.Label(account, text="TELEGRAM · 本地工作区", style="Workspace.TLabel").pack(side="right")
        self.account_button = ttk.Button(account, text="切换账号", style="Small.TButton", command=self.open_login_settings)
        self.account_button.pack(side="right", padx=(12, 14))
        ttk.Label(account, textvariable=self.account_status, style="Hint.TLabel").pack(side="right")
        self.connection_indicator = ttk.Label(account, text="●", style="Offline.TLabel")
        self.connection_indicator.pack(side="right", padx=(0, 8))

        Frame(shell, background=DARK_BORDER_SOFT, height=1).pack(fill="x")

        panel = Frame(shell, background=DARK_SURFACE, highlightbackground=DARK_BORDER, highlightthickness=1)
        panel.pack(fill="both", expand=True, padx=22, pady=(16, 0))
        panel.grid_columnconfigure(0, weight=10, uniform="panel_cols")
        panel.grid_columnconfigure(2, weight=11, uniform="panel_cols")
        panel.grid_columnconfigure(4, weight=13, uniform="panel_cols")
        panel.grid_rowconfigure(0, weight=1)
        for column in (1, 3):
            Frame(panel, background=DARK_BORDER_SOFT, width=1).grid(row=0, column=column, sticky="ns", pady=16)

        col1 = Frame(panel, background=DARK_SURFACE)
        col1.grid(row=0, column=0, sticky="nsew", padx=(20, 14), pady=16)
        head1 = Frame(col1, background=DARK_SURFACE)
        head1.pack(fill="x", pady=(0, 14))
        ttk.Label(head1, text="01", style="PanelNo.TLabel").pack(side="left")
        ttk.Label(head1, text="选择群组", style="PanelTitle.TLabel").pack(side="left", padx=(10, 0), pady=(5, 0))
        ttk.Label(col1, text="资料群组", style="PanelSection.TLabel").pack(anchor="w", pady=(0, 6))
        list_wrap = Frame(col1, background=DARK_SURFACE)
        list_wrap.pack(fill="x")
        self.group_list = ttk.Treeview(
            list_wrap,
            columns=("pad", "name"),
            show="headings",
            selectmode="browse",
            height=7,
            style="DarkList.Treeview",
            takefocus=False,
        )
        self.group_list.heading("pad", text="")
        self.group_list.column("pad", anchor="w", stretch=False, width=12, minwidth=12)
        self.group_list.heading("name", text="")
        self.group_list.column("name", anchor="w", stretch=True, width=180)
        self.group_list.tag_configure("captured", foreground=DARK_DANGER)
        self.group_list.pack(side="left", fill="both", expand=True)
        list_scroll = ttk.Scrollbar(
            list_wrap, orient="vertical", style="Dark.Vertical.TScrollbar", command=self.group_list.yview
        )
        list_scroll.pack(side="right", fill="y", padx=(6, 0))
        self.group_list.configure(yscrollcommand=list_scroll.set)
        self.group_list.bind("<<TreeviewSelect>>", self.on_group_list_selected)
        ttk.Label(col1, text="工具", style="PanelSection.TLabel").pack(anchor="w", pady=(16, 6))
        self.group_button = ttk.Button(col1, text="群组设置", style="Tool.TButton", command=self.open_group_settings)
        self.group_button.pack(fill="x", pady=4)
        self.status_button = ttk.Button(col1, text="查看抓取状态", style="Tool.TButton", command=self.open_capture_status)
        self.status_button.pack(fill="x", pady=4)
        self.open_button = ttk.Button(col1, text="打开结果", style="Tool.TButton", command=self.open_output)
        self.open_button.pack(fill="x", pady=4)
        self.settings_button = ttk.Button(col1, text="设置", style="Tool.TButton", command=self.open_login_settings)
        self.settings_button.pack(fill="x", pady=4)

        col2 = Frame(panel, background=DARK_SURFACE)
        col2.grid(row=0, column=2, sticky="nsew", padx=16, pady=16)
        head2 = Frame(col2, background=DARK_SURFACE)
        head2.pack(fill="x", pady=(0, 14))
        ttk.Label(head2, text="02", style="PanelNo.TLabel").pack(side="left")
        ttk.Label(head2, text="核对与提取", style="PanelTitle.TLabel").pack(side="left", padx=(10, 0), pady=(5, 0))

        ttk.Label(col2, text="提取账号", style="PanelField.TLabel").pack(anchor="w")
        account_entry = ttk.Entry(col2, textvariable=self.account_status, state="readonly", style="Dark.TEntry", font=FORM_FONT)
        account_entry.pack(fill="x", pady=(4, 10))
        ttk.Label(col2, text="抓取日期", style="PanelField.TLabel").pack(anchor="w")
        day_entry = ttk.Entry(col2, textvariable=self.day, style="Dark.TEntry", font=FORM_FONT)
        day_entry.pack(fill="x", pady=(4, 10))
        ttk.Label(col2, text="时间范围", style="PanelField.TLabel").pack(anchor="w")
        time_entry = ttk.Entry(col2, textvariable=self.time_range, state="readonly", style="Dark.TEntry", font=FORM_FONT)
        time_entry.pack(fill="x", pady=(4, 10))
        ttk.Label(col2, text="备注 JSON", style="PanelField.TLabel").pack(anchor="w")
        notes_row = Frame(col2, background=DARK_SURFACE)
        notes_row.pack(fill="x", pady=(4, 10))
        notes_entry = ttk.Entry(notes_row, textvariable=self.notes_path, state="readonly", style="Dark.TEntry", font=FORM_FONT)
        notes_entry.pack(side="left", fill="x", expand=True)
        notes_button = ttk.Button(notes_row, text="打开", style="Small.TButton", command=self.open_notes_file)
        notes_button.pack(side="left", padx=(8, 0))
        ttk.Label(col2, text="保存位置", style="PanelField.TLabel").pack(anchor="w")
        output_row = Frame(col2, background=DARK_SURFACE)
        output_row.pack(fill="x", pady=(4, 10))
        output_entry = ttk.Entry(output_row, textvariable=self.output_path, style="Dark.TEntry", font=FORM_FONT)
        output_entry.pack(side="left", fill="x", expand=True)
        output_button = ttk.Button(output_row, text="浏览", style="Small.TButton", command=self.choose_output)
        output_button.pack(side="left", padx=(8, 0))
        self.first_capture_status_label = ttk.Label(
            col2, textvariable=self.first_capture_status, style="FirstCapturePending.TLabel"
        )
        self.first_capture_status_label.pack(anchor="w", pady=(4, 10))
        self.retry_button = ttk.Button(col2, text="复抓", style="Tool.TButton", command=self.retry_capture)
        self.retry_button.pack(fill="x", side="bottom")
        self.start_button = ttk.Button(col2, text="开始精准提取", style="Accent.TButton", command=self.start_download)
        self.start_button.pack(fill="x", side="bottom", pady=(0, 18))

        col3 = Frame(panel, background=DARK_SURFACE)
        col3.grid(row=0, column=4, sticky="nsew", padx=(14, 20), pady=16)
        head3 = Frame(col3, background=DARK_SURFACE)
        head3.pack(fill="x", pady=(0, 14))
        ttk.Label(head3, text="03", style="PanelNo.TLabel").pack(side="left")
        ttk.Label(head3, text="结果与日志", style="PanelTitle.TLabel").pack(side="left", padx=(10, 0), pady=(5, 0))
        ttk.Label(col3, text="任务状态", style="PanelField.TLabel").pack(anchor="w")
        task_entry = ttk.Entry(col3, textvariable=self.task_status, state="readonly", style="Dark.TEntry", font=FORM_FONT)
        task_entry.pack(fill="x", pady=(4, 10))
        self.log_box = ScrolledText(
            col3,
            height=10,
            font=("Microsoft YaHei UI", 12),
            state="disabled",
            background=DARK_LOG_BG,
            foreground=DARK_TEXT,
            insertbackground=DARK_TEXT,
            selectbackground="#4A3A1E",
            selectforeground="#FFFFFF",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=DARK_BORDER,
            padx=12,
            pady=8,
        )
        self.log_box.pack(fill="both", expand=True)
        try:
            self.log_box.vbar.configure(background=DARK_FIELD, troughcolor=DARK_LOG_BG, activebackground="#3B424C")
        except TclError:
            pass
        self.clear_button = ttk.Button(col3, text="清除结果", style="Danger.TButton", command=self.clear_output)
        self.clear_button.pack(fill="x", pady=(10, 0))

        Frame(shell, background=DARK_BORDER_SOFT, height=1).pack(fill="x", pady=(16, 0))
        bottom = Frame(shell, background=DARK_BOTTOM)
        bottom.pack(fill="x")
        bottom_row = Frame(bottom, background=DARK_BOTTOM)
        bottom_row.pack(fill="x", padx=22, pady=9)
        ttk.Label(bottom_row, text="处理进度", style="BottomKey.TLabel").pack(side="left")
        self.progress_bar = ttk.Progressbar(
            bottom_row, mode="determinate", maximum=100, length=240, style="Dark.Horizontal.TProgressbar"
        )
        self.progress_bar.pack(side="left", padx=(8, 10))
        ttk.Label(bottom_row, textvariable=self.progress_summary, style="BottomValue.TLabel").pack(side="left")
        ttk.Label(bottom_row, text="关键词为空时提取该群当天全部图片", style="BottomHint.TLabel").pack(side="right")

        self.busy_widgets.extend(
            [
                self.group_button,
                self.status_button,
                self.retry_button,
                day_entry,
                time_entry,
                notes_entry,
                notes_button,
                output_entry,
                output_button,
                self.start_button,
                self.open_button,
                self.settings_button,
                self.clear_button,
                self.account_button,
            ]
        )
        self.log("软件就绪。备注 JSON 的 keywords 保存关键词；为空时提取该群当天全部图片。")
        self.day.trace_add("write", lambda *_: self.update_first_capture_status())
        self.output_path.trace_add("write", lambda *_: self.update_first_capture_status())

    def _entry_row(self, parent, row: int, label: str, variable: StringVar, show: str | None = None) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
        entry = ttk.Entry(parent, textvariable=variable, show=show, style="Dark.TEntry", font=FORM_FONT)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        self.busy_widgets.append(entry)

    def _path_row(self, parent, row: int, label: str, variable: StringVar, command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
        entry = ttk.Entry(parent, textvariable=variable, style="Dark.TEntry", font=FORM_FONT)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        button = ttk.Button(parent, text="浏览…", command=command)
        button.grid(row=row, column=2, padx=(8, 0), pady=4)
        self.busy_widgets.extend([entry, button])

    def reload_group_profiles(self, preferred: str = "") -> None:
        names = [item["name"] for item in self.settings.get("groups", [])]
        self.group_list.delete(*self.group_list.get_children())
        for name in names:
            self.group_list.insert("", "end", iid=name, values=("", name))
        selected = preferred if preferred in names else self.settings.get("selected_group", "")
        if selected not in names:
            selected = names[0] if names else ""
        self.chat.set(selected)
        if selected in names:
            self.group_list.selection_set(selected)
            self.group_list.see(selected)
        self._set_log_group(selected or "系统")
        self.settings["selected_group"] = selected
        self.notes_path.set(str(group_notes_path(self.program_root, selected)) if selected else "请点击“群组设置”添加群配置")
        profile = self.selected_group_profile(required=False)
        self.time_range.set(
            f"{profile['start_time']} ～ {profile['end_time']}" if profile else "请先添加群配置"
        )
        self.update_retry_button()
        self.update_first_capture_status()
        _write_group_settings(self.program_root, self.settings)

    def selected_group_profile(self, required: bool = True) -> dict | None:
        name = self.chat.get()
        item = next((item for item in self.settings.get("groups", []) if item["name"] == name), None)
        if required and not item:
            raise ValueError("请先点击“设置…”添加并选择群配置")
        return item

    def selected_group_address(self) -> str:
        return self.selected_group_profile()["address"]

    def on_group_list_selected(self, _event=None) -> None:
        if self._busy:
            current = self.chat.get()
            # 任务进行中若直接 selection_set，会再次触发本事件，陷入死循环并卡死窗口。
            if current and self.group_list.exists(current) and self.group_list.selection() != (current,):
                self.group_list.selection_set(current)
            return
        selection = self.group_list.selection()
        if not selection:
            return
        self.chat.set(selection[0])
        self.on_group_selected()

    def on_group_selected(self, _event=None) -> None:
        selected = self.chat.get()
        self._set_log_group(selected or "系统")
        self.settings["selected_group"] = selected
        self.notes_path.set(str(group_notes_path(self.program_root, selected)))
        profile = self.selected_group_profile()
        self.time_range.set(f"{profile['start_time']} ～ {profile['end_time']}")
        self.update_retry_button()
        self.update_first_capture_status()
        _write_group_settings(self.program_root, self.settings)

    def refresh_group_colors(self) -> None:
        names = [item["name"] for item in self.settings.get("groups", [])]
        if not names:
            return
        captured: set[str] = set()
        try:
            output = Path(self.output_path.get().strip().strip('"')).expanduser()
            target_day = parse_day(self.day.get())
        except (OSError, ValueError):
            pass
        else:
            captured = {name for name in names if today_result_exists(output, target_day, name)}
        for name in names:
            if self.group_list.exists(name):
                self.group_list.item(name, tags=("captured",) if name in captured else ())

    def update_first_capture_status(self) -> bool:
        group_name = self.chat.get().strip()
        captured = False
        if group_name:
            try:
                target_day = parse_day(self.day.get())
                output = Path(self.output_path.get().strip().strip('"')).expanduser()
                captured = today_result_exists(output, target_day, group_name)
            except (OSError, RuntimeError, ValueError):
                captured = False
        self.first_capture_status.set("今日已首抓" if captured else "今日未首抓")
        self.first_capture_status_label.configure(
            style="FirstCaptureDone.TLabel" if captured else "FirstCapturePending.TLabel"
        )
        self.refresh_group_colors()
        return captured

    def update_retry_button(self) -> None:
        self.retry_button.configure(state="normal" if self.chat.get() else "disabled")

    def open_group_settings(self) -> None:
        GroupSettingsDialog(self)

    def refresh_account_profiles(self) -> bool:
        try:
            self.account_profiles = load_account_profiles(self.app_data)
        except (OSError, ValueError) as exc:
            self.account_profiles = []
            self.account_status.set("账号列表读取失败，原文件已保留")
            self.log(f"【账号】账号列表读取失败（{type(exc).__name__}），未覆盖文件")
            self._account_load_error = True
            return False
        self._account_load_error = False
        choices = [f"{index + 1}. {profile['name']}" for index, profile in enumerate(self.account_profiles)]
        combo = getattr(self, "account_combo", None)
        if combo is not None and combo.winfo_exists():
            combo.configure(values=choices)
        self.account_choice.set(next((choices[index] for index, profile in enumerate(self.account_profiles)
                                      if profile["id"] == self.selected_account_id), ""))
        return True

    def _clear_login_state(self) -> None:
        self.logged_in = False
        self.active_account_id = ""
        self.active_user_id = None
        self.account_status.set("未登录，请选择账号后点击登录")
        self.connection_indicator.configure(style="Offline.TLabel")

    def selected_credentials(self) -> tuple[int, str, str]:
        if not any(profile["id"] == self.selected_account_id for profile in self.account_profiles):
            raise ValueError("请先选择账号，或点击添加账号")
        saved = load_saved_credentials(account_directory(self.app_data, self.selected_account_id) / "credentials.bin")
        if saved is None:
            raise ValueError("所选账号的登录配置不存在，原会话文件未改动")
        return saved

    def select_account(self, account_id: str, *, remember: bool = True) -> None:
        if self._busy:
            return
        self._clear_login_state()
        self.selected_account_id = ""
        self.adding_account = False
        for variable in (self.api_id, self.api_hash, self.phone, self.account_name):
            variable.set("")
        profile = next((item for item in self.account_profiles if item["id"] == account_id), None)
        if profile is not None:
            self.selected_account_id = account_id
            try:
                api_id, api_hash, phone = self.selected_credentials()
                if remember:
                    save_selected_account(self.app_data, account_id)
            except (OSError, ValueError):
                self.selected_account_id = ""
                if self._workflow_mode:
                    self._workflow_fail("无法读取默认账号配置；原文件和会话均已保留")
                else:
                    messagebox.showwarning("账号配置不可用", "无法读取登录配置或保存默认账号，本次选择未生效；原文件和会话均已保留。")
            else:
                self.api_id.set(str(api_id))
                self.api_hash.set(api_hash)
                self.phone.set(phone)
                self.account_name.set(profile["name"])
                self.account_status.set(f"已选择：{profile['name']}，请点击登录")
        self.refresh_account_profiles()
        self._update_account_form()

    def on_account_selected(self, _event=None) -> None:
        index = self.account_combo.current()
        if 0 <= index < len(self.account_profiles):
            self.select_account(self.account_profiles[index]["id"])

    def begin_add_account(self) -> None:
        if self._busy or self._account_load_error:
            return
        self._clear_login_state()
        self.selected_account_id = ""
        self.adding_account = True
        self.account_choice.set("")
        self.account_name.set(f"账号 {len(self.account_profiles) + 1}")
        for variable in (self.api_id, self.api_hash, self.phone):
            variable.set("")
        self.account_status.set("添加账号：填写后点击保存并登录")
        self._update_account_form()

    def _update_account_form(self) -> None:
        window = getattr(self, "login_settings_window", None)
        if window is None or not window.winfo_exists():
            return
        editable = not self._busy and (self.adding_account or bool(self.selected_account_id))
        for entry in self.account_fields:
            entry.configure(state="normal" if editable else "disabled")
        if editable and not self.adding_account:
            self.phone_entry.configure(state="readonly")
        self.account_combo.configure(state="disabled" if self._busy else "readonly")
        self.add_account_button.configure(state="disabled" if self._busy or self._account_load_error else "normal")
        self.login_submit_button.configure(
            state="normal" if editable else "disabled",
            text="保存并登录" if self.adding_account else "登录所选账号",
        )
        self.login_close_button.configure(state="disabled" if self._busy else "normal")

    def close_login_settings(self) -> None:
        if not self._busy:
            self.login_settings_window.destroy()

    def open_login_settings(self) -> None:
        if self._busy:
            return
        existing = getattr(self, "login_settings_window", None)
        if existing is not None and existing.winfo_exists():
            self._center_dialog(existing, 760, 640)
            existing.lift()
            existing.focus_force()
            return

        window = Toplevel(self.root)
        self.login_settings_window = window
        window.title("选择 Telegram 账号")
        window.resizable(False, False)
        window.configure(background=DARK_BG)
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", self.close_login_settings)

        frame = ttk.Frame(window, style="Dark.TFrame", padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="选择账号后登录", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text="选择后在本机记住，下次自动登录此账号。新账号请点击“添加账号”。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(5, 14))

        selector = ttk.Frame(frame, style="Dark.TFrame")
        selector.pack(fill="x", pady=(0, 12))
        self.account_combo = ttk.Combobox(selector, textvariable=self.account_choice,
                                        state="readonly", style="Dark.TCombobox", font=FORM_FONT)
        self.account_combo.pack(side="left", fill="x", expand=True)
        self.account_combo.bind("<<ComboboxSelected>>", self.on_account_selected)
        self.add_account_button = ttk.Button(selector, text="添加账号", command=self.begin_add_account)
        self.add_account_button.pack(side="right", padx=(12, 0))

        form = ttk.Frame(frame, style="Card.TFrame", padding=18)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.account_fields = []
        for row, (label, variable) in enumerate((("账号名称", self.account_name), ("API ID", self.api_id),
                                                ("API Hash", self.api_hash), ("手机号", self.phone))):
            ttk.Label(form, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
            entry = ttk.Entry(form, textvariable=variable, style="Dark.TEntry", font=FORM_FONT,
                              show="●" if label == "API Hash" else "")
            entry.grid(row=row, column=1, columnspan=1 if label == "API ID" else 2, sticky="ew", pady=5)
            self.account_fields.append(entry)
        self.phone_entry = self.account_fields[-1]
        api_button = ttk.Button(form, text="申请 API", command=lambda: webbrowser.open("https://my.telegram.org/"))
        api_button.grid(row=1, column=2, padx=(8, 0), pady=5)

        status = ttk.Frame(frame, style="Dark.TFrame")
        status.pack(fill="x", pady=(14, 12))
        ttk.Label(status, text="当前状态", style="Hint.TLabel").pack(side="left")
        ttk.Label(status, textvariable=self.account_status, wraplength=500).pack(side="right")

        display = ttk.Frame(frame, style="Dark.TFrame")
        display.pack(fill="x", pady=(0, 14))
        ttk.Label(display, text="窗口分辨率", style="Hint.TLabel").pack(side="left")
        self._preset_buttons = {}
        for preset in ("2k", "1080"):
            button = ttk.Button(
                display, text=preset.upper(), style="Preset.TButton",
                command=lambda value=preset: self.choose_window_preset(value),
            )
            button.pack(side="left", padx=(10, 0))
            self._preset_buttons[preset] = button
        ttk.Label(display, text="切换后自动重启生效", style="Hint.TLabel").pack(side="left", padx=(12, 0))
        self._refresh_preset_buttons()

        buttons = ttk.Frame(frame, style="Dark.TFrame")
        buttons.pack(fill="x")
        self.login_submit_button = ttk.Button(
            buttons, text="登录所选账号", style="Accent.TButton", command=self.login
        )
        self.login_submit_button.pack(side="left")
        self.login_close_button = ttk.Button(buttons, text="暂不登录", command=self.close_login_settings)
        self.login_close_button.pack(side="right")
        if self.refresh_account_profiles() and not self.account_profiles:
            self.begin_add_account()
        self._update_account_form()
        self._center_dialog(window, 760, 640)
        window.bind("<Escape>", lambda _event: self.close_login_settings())
        window.lift()
        self.account_combo.focus_set()
        window.after_idle(lambda: self._enable_dark_title_bar(window))

    def open_notes_file(self) -> None:
        if not self.chat.get():
            messagebox.showwarning("尚未配置", "请先点击“设置…”添加群配置")
            return
        path = group_notes_path(self.program_root, self.chat.get())
        if not path.exists():
            write_notes_config(path, [])
        self._open_with_shell(path)

    def choose_output(self) -> None:
        selected = filedialog.askdirectory(title="选择保存目录")
        if selected:
            self.output_path.set(selected)
            self.settings["output_path"] = selected
            _write_group_settings(self.program_root, self.settings)

    def open_output(self) -> None:
        path = Path(self.output_path.get()).expanduser()
        try:
            group = self.selected_group_profile(required=False)
            target_day = parse_day(self.day.get())
        except ValueError:
            group = None
        if group:
            path = dated_group_output(path, target_day, group["name"])
        if not path.is_dir():
            messagebox.showinfo("结果目录不存在", "当前群和日期还没有抓取结果。")
            return
        self._open_with_shell(path)

    def open_capture_status(self) -> None:
        group_name = self.chat.get().strip()
        if not group_name:
            messagebox.showwarning("尚未配置", "请先点击“群组设置”添加群配置")
            return
        path = capture_status_path(self.status_root, group_name)
        if not path.exists():
            messagebox.showinfo("暂无抓取状态", f"该群还没有抓取状态记录：\n{path}")
            return
        self._open_with_shell(path)

    def _initial_window_preset(self) -> str:
        preset = load_window_preset(self.app_data)
        if preset:
            return preset
        return "2k" if self.root.winfo_screenwidth() >= 2000 else "1080"

    def _window_size_for_preset(self, preset: str) -> tuple[int, int]:
        width, height = WINDOW_PRESETS.get(preset, WINDOW_PRESETS["1080"])
        width = min(width, max(self.root.winfo_screenwidth() - 40, 800))
        height = min(height, max(self.root.winfo_screenheight() - 80, 600))
        return width, height

    def restart_application(self) -> None:
        if getattr(sys, "frozen", False):
            command = [sys.executable]
            workdir = Path(sys.executable).resolve().parent
        else:
            command = [sys.executable, str(Path(__file__).resolve())]
            workdir = Path(__file__).resolve().parent
        subprocess.Popen(command, cwd=str(workdir))
        self.root.destroy()

    def choose_window_preset(self, preset: str) -> None:
        if preset not in WINDOW_PRESETS:
            return
        if self._busy:
            messagebox.showwarning("任务进行中", "请等当前任务结束后再切换窗口分辨率")
            return
        if preset == self.window_preset:
            messagebox.showinfo("提示", f"当前窗口已经是 {preset.upper()} 预设")
            return
        try:
            save_window_preset(self.app_data, preset)
        except (OSError, ValueError) as exc:
            messagebox.showwarning("无法保存", str(exc))
            return
        messagebox.showinfo("已保存", "窗口分辨率已保存，软件将自动重启生效")
        self.restart_application()

    def _refresh_preset_buttons(self) -> None:
        for preset, button in getattr(self, "_preset_buttons", {}).items():
            if button.winfo_exists():
                button.configure(
                    style="PresetActive.TButton" if preset == self.window_preset else "Preset.TButton",
                    text=preset.upper(),
                )

    def clear_output(self) -> None:
        path = Path(self.output_path.get().strip().strip('"')).expanduser().resolve()
        protected = [Path.home().resolve(), self.program_root.resolve(), self.app_data.resolve()]
        if path == Path(path.anchor) or any(path == item or path in item.parents for item in protected):
            messagebox.showwarning("无法清除", "为避免误删，不能清除磁盘根目录、用户目录或软件目录")
            return
        path.mkdir(parents=True, exist_ok=True)
        removed = 0
        try:
            for item in path.iterdir():
                if item.is_symlink() or item.is_file():
                    item.unlink()
                else:
                    shutil.rmtree(item)
                removed += 1
        except OSError as exc:
            messagebox.showerror("清除失败", str(exc))
            self.task_status.set("清除失败")
            return
        self.task_status.set(f"已清除结果：{removed} 项")
        self.update_first_capture_status()
        self.log(f"已清空结果目录：{path}（{removed} 项）")

    def retry_unmatched(self) -> None:
        if self.chat.get() != SPECIAL_RETRY_GROUP:
            return
        try:
            target_day = parse_day(self.day.get())
        except ValueError:
            messagebox.showwarning("日期错误", "日期格式应为 YYYY-MM-DD")
            return
        output = Path(self.output_path.get().strip().strip('"')).expanduser().resolve()
        unmatched_file = dated_group_output(output, target_day, SPECIAL_RETRY_GROUP) / "未匹配备注.txt"
        try:
            notes = load_notes(unmatched_file)
        except FileNotFoundError:
            messagebox.showwarning("没有失败名单", "尚未找到嫣然群的未匹配备注文件")
            return
        except (OSError, ValueError) as exc:
            messagebox.showwarning("没有需要复抓的备注", str(exc))
            return
        self._set_log_group(SPECIAL_RETRY_GROUP)
        self.log(f"【复抓】开始复抓嫣然群未匹配备注：{len(notes)} 条。")
        self.start_download(notes_override=notes, retry=True)

    def retry_capture(self) -> None:
        group_name = self.chat.get().strip()
        if not group_name:
            return
        self._set_log_group(group_name)
        try:
            target_day = parse_day(self.day.get())
            output = Path(self.output_path.get().strip().strip('"')).expanduser().resolve()
            group_output = dated_group_output(output, target_day, group_name)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.log(f"【状态】复抓状态读取失败：{capture_status_path(self.status_root, group_name)}（{type(exc).__name__}）")
            messagebox.showwarning("无法读取抓取状态", str(exc))
            return
        self.update_first_capture_status()
        if not group_output.is_dir():
            self.log(f"【复抓】今日结果文件夹不存在，禁止复抓：{group_output}")
            messagebox.showwarning("请先首抓", "请先首抓")
            return
        try:
            status = load_capture_status(capture_status_path(self.status_root, group_name))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.log(f"【状态】复抓状态读取失败：{capture_status_path(self.status_root, group_name)}（{type(exc).__name__}）")
            messagebox.showwarning("无法读取抓取状态", str(exc))
            return
        first_capture = (
            status is None
            or status.get("date") != target_day.isoformat()
            or not group_output.is_dir()
        )
        if first_capture:
            self.log(f"【复抓】未找到{target_day}的有效状态，按当日首次抓取处理。")
        else:
            pending = sum(
                1
                for item in status.get("remarks", {}).values()
                if item.get("status") != "已完成"
            )
            self.log(f"【复抓】按抓取状态复抓{group_name}：{pending} 个备注待检查。")
        self.start_download(retry=True)

    def _set_log_group(self, group_name: str) -> None:
        group_name = group_name.strip() or "系统"
        if group_name != self._log_group:
            self._log_group = group_name
            self._log_day = None

    def _rotate_runtime_log(self, target_day: date | None = None) -> Path:
        today = target_day or datetime.now(CN_TZ).date()
        with self._log_lock:
            path = clear_old_runtime_logs(self.log_root, today, self._log_group)
            self._log_day = today
        return path

    def _load_runtime_log(self) -> None:
        path = self._rotate_runtime_log()
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        if content:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", content)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

    def _schedule_log_rollover(self) -> None:
        now = datetime.now(CN_TZ)
        tomorrow = datetime.combine(now.date() + timedelta(days=1), clock_time.min, CN_TZ)
        delay = max(1000, int((tomorrow - now).total_seconds() * 1000))
        self._log_rollover_id = self.root.after(delay, self._runtime_log_midnight)

    def _runtime_log_midnight(self) -> None:
        self._rotate_runtime_log()

        def clear_view() -> None:
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.configure(state="disabled")

        self.root.after(0, clear_view)
        self._schedule_log_rollover()

    def _append_log_file(self, text: str) -> None:
        with self._log_lock:
            try:
                now = datetime.now(CN_TZ)
                path = self._rotate_runtime_log(now.date())
                stamp = now.strftime("[%H:%M:%S] ")
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(stamp + text + "\n")
            except OSError:
                pass
            try:
                if self._workflow_log_path is not None:
                    self._workflow_log_path.parent.mkdir(parents=True, exist_ok=True)
                    with self._workflow_log_path.open("a", encoding="utf-8") as handle:
                        handle.write(text + "\n")
            except OSError:
                pass

    def log(self, text: str) -> None:
        if not text.startswith("【"):
            text = "【系统】" + text
        self._append_log_file(text)

        def append() -> None:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", text + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        self._post_to_main(append)

    def set_busy(self, busy: bool, status: str = "") -> None:
        if not busy:
            if status == "操作失败" and self._capture_started is not None:
                status += f"｜已耗时 {format_duration(time.monotonic() - self._capture_started)}"
            self._stop_capture_progress()
        if busy and not self._busy:
            self._busy_widget_states = {widget: str(widget.cget("state")) for widget in self.busy_widgets}
        self._busy = busy
        for widget in self.busy_widgets:
            widget.configure(state="disabled" if busy else self._busy_widget_states.get(widget, "normal"))
        self._update_account_form()
        if status:
            self.task_status.set(status)
        if self._capture_started is not None:
            self._render_capture_progress()

    def _start_capture_progress(self) -> None:
        self._stop_capture_progress()
        self._capture_started = time.monotonic()
        self._capture_progress = (0, 0, "准备抓取…", None)
        self._tick_capture_progress()

    def _on_capture_window_destroyed(self, event) -> None:
        if event.widget is self.root:
            self._watchdog_stop.set()
            self._stop_capture_progress()
            if self._ui_pump_id is not None:
                try:
                    self.root.after_cancel(self._ui_pump_id)
                except TclError:
                    pass
                self._ui_pump_id = None

    def _post_to_main(self, callback) -> None:
        """Deliver UI work from worker threads without touching Tk off the main thread."""
        if threading.current_thread() is threading.main_thread():
            self.root.after(0, callback)
        else:
            self._ui_queue.put(callback)

    def _start_freeze_watchdog(self) -> None:
        """Record what the UI thread was doing when Windows declares it unresponsive."""
        self._ui_heartbeat = time.monotonic()

        def watch() -> None:
            reported_at = 0.0
            while not self._watchdog_stop.wait(UI_HEARTBEAT_SECONDS):
                stalled = time.monotonic() - self._ui_heartbeat
                if stalled < UI_FREEZE_SECONDS:
                    reported_at = 0.0
                    continue
                if reported_at and stalled - reported_at < UI_FREEZE_DUMP_COOLDOWN:
                    continue
                reported_at = stalled
                self._freeze_dump(stalled)

        threading.Thread(target=watch, name="ui-freeze-watchdog", daemon=True).start()

    def _freeze_dump(self, stalled: float) -> None:
        self._append_log_file(f"【诊断】界面线程无响应 {stalled:.0f} 秒，线程堆栈：\n{thread_stack_dump()}")
        self.log(f"【诊断】界面线程曾无响应 {stalled:.0f} 秒，堆栈已写入运行日志")

    def _open_with_shell(self, target, failure_title: str = "打开失败") -> None:
        """ShellExecute can wait on a busy Shell, so never call it on the UI thread."""
        def worker() -> None:
            try:
                os.startfile(target)
            except OSError as exc:
                text = str(exc) or "无法打开该文件或目录"
                self._post_to_main(lambda message=text: messagebox.showerror(failure_title, message))

        threading.Thread(target=worker, daemon=True).start()

    def _pump_ui_callbacks(self) -> None:
        self._ui_heartbeat = time.monotonic()
        if self._ui_pump_id is not None:
            try:
                self.root.after_cancel(self._ui_pump_id)
            except TclError:
                pass
            self._ui_pump_id = None
        try:
            while True:
                callback = self._ui_queue.get_nowait()
                try:
                    callback()
                except Exception:
                    pass
        except queue.Empty:
            pass
        try:
            self._ui_pump_id = self.root.after(100, self._pump_ui_callbacks)
        except TclError:
            self._ui_pump_id = None

    def _workflow_fail(self, detail: str) -> None:
        if self._workflow_exit_code is not None:
            return
        self._workflow_exit_code = 1
        self.log(f"【工作流】失败：{detail}")
        self.root.after(0, self.root.destroy)

    def _workflow_finish(self, code: int) -> None:
        if self._workflow_exit_code is not None:
            return
        self._workflow_exit_code = code
        self.log("【工作流】单群抓取完成" if code == 0 else "【工作流】单群抓取失败")
        self.root.after(0, self.root.destroy)

    def run_workflow_group(self, group_name: str) -> None:
        """Run one configured group without opening dialogs; used by scheduled jobs."""
        self._workflow_mode = True
        self._workflow_group = group_name.strip()
        if not self._workflow_group:
            self._workflow_fail("群名不能为空")
            return
        names = {item["name"] for item in self.settings.get("groups", [])}
        if self._workflow_group not in names:
            self._workflow_fail(f"未找到群配置：{self._workflow_group}")
            return
        try:
            self.reload_group_profiles(self._workflow_group)
            account_id = load_selected_account(self.app_data)
            if not account_id:
                raise ValueError("未找到默认账号，请先在 GUI 中选择并登录账号")
            self.select_account(account_id, remember=False)
            if self.selected_account_id != account_id:
                raise ValueError("默认账号配置不可用")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._workflow_fail(str(exc))
            return
        self.login(automatic=True, on_failure=self._workflow_fail)

    def _workflow_start_capture(self) -> None:
        if self._workflow_exit_code is None and self._workflow_mode:
            self.start_download()

    def _stop_capture_progress(self) -> None:
        self._capture_generation += 1
        self._capture_started = None
        if self._capture_timer is not None:
            self.root.after_cancel(self._capture_timer)
            self._capture_timer = None
        try:
            self.progress_bar.configure(value=0)
            self.progress_summary.set("")
        except (AttributeError, TclError):
            pass

    def _show_progress_bar(self, current: int, total: int) -> None:
        try:
            if total > 0 and current > 0:
                self.progress_bar.configure(value=min(100, current * 100 / total))
                self.progress_summary.set(f"{current} / {total}")
            else:
                self.progress_bar.configure(value=0)
                self.progress_summary.set("")
        except TclError:
            pass

    def _render_capture_progress(self) -> None:
        current, total, status, phase_started = self._capture_progress
        now = time.monotonic()
        effective = current if phase_started is not None else 0
        self.task_status.set(format_processing_status(
            effective, total,
            max(0, now - phase_started) if phase_started is not None else 0,
            status=status, total_elapsed=now - self._capture_started,
        ))
        self._show_progress_bar(effective, total)

    def _tick_capture_progress(self) -> None:
        self._capture_timer = None
        if self._capture_started is not None:
            self._render_capture_progress()
            self._capture_timer = self.root.after(1000, self._tick_capture_progress)

    def set_progress(self, current: int, total: int, status: str, *, phase_started: float | None = None) -> None:
        generation = self._capture_generation

        def update() -> None:
            # Queued worker callbacks must not overwrite completion or a newer task.
            if generation != self._capture_generation:
                return
            if self._capture_started is None:
                self.task_status.set(status)
            else:
                self._capture_progress = (current, total, status, phase_started)
                self._render_capture_progress()

        self._post_to_main(update)

    def run_worker(self, operation, on_success, on_failure=None) -> None:
        if self._busy:
            return
        self.set_busy(True, "准备中…")

        def worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = operation()
            except Exception as exc:
                error_text = str(exc) or "未知错误"
                self.log(f"【错误】任务中止：{type(exc).__name__}: {exc}")
                def fail(text=error_text) -> None:
                    self.set_busy(False, "操作失败")
                    if not self._workflow_mode:
                        messagebox.showerror("操作失败", text)
                    if on_failure is not None:
                        on_failure(text) if self._workflow_mode else on_failure()
                    elif self._workflow_mode:
                        self._workflow_fail(text)
                self._post_to_main(fail)
                return
            finally:
                loop.close()
                asyncio.set_event_loop(None)
            def finish() -> None:
                try:
                    on_success(result)
                finally:
                    self.set_busy(False)
            self._post_to_main(finish)

        threading.Thread(target=worker, daemon=True).start()

    def credentials(self) -> tuple[int, str, str]:
        api_id = self.api_id.get().strip()
        api_hash = self.api_hash.get().strip()
        phone = self.phone.get().strip()
        if not api_id.isascii() or not api_id.isdigit() or int(api_id) <= 0 or not api_hash or not phone:
            raise ValueError("请填写正确的 API ID、API Hash 和手机号")
        return int(api_id), api_hash, normalize_account_phone(phone)

    def ask_secret(self, title: str, prompt: str, masked: bool = False) -> str:
        event = threading.Event()
        answer: dict[str, str | None] = {"value": None}

        def show_dialog() -> None:
            answer["value"] = simpledialog.askstring(title, prompt, parent=self.root, show="●" if masked else None)
            event.set()

        self._post_to_main(show_dialog)
        event.wait()
        value = answer["value"]
        if not value:
            raise RuntimeError("登录已取消")
        return value.strip()

    def login(self, *, automatic: bool = False, on_failure=None) -> None:
        if self._busy:
            return
        if self._account_load_error:
            if self._workflow_mode:
                self._workflow_fail("账号列表读取失败，无法恢复默认账号")
            return
        try:
            if not self.adding_account and not self.selected_account_id:
                raise ValueError("请先选择账号，或点击添加账号")
            api_id, api_hash, phone = self.credentials()
            name = normalized(self.account_name.get())
            if not name or len(name) > 80:
                raise ValueError("账号名称须为 1～80 个字")
            if self.adding_account:
                profile = create_account_profile(self.app_data, name, api_id, api_hash, phone)
                self.selected_account_id = profile["id"]
                self.adding_account = False
                self.refresh_account_profiles()
                self._update_account_form()
            else:
                saved = self.selected_credentials()
                if normalize_account_phone(saved[2]) != phone:
                    raise ValueError("已有账号不能更换手机号，请使用添加账号")
                profile = dict(next(item for item in self.account_profiles if item["id"] == self.selected_account_id))
            profile["name"] = name
            directory = account_directory(self.app_data, profile["id"])
            if automatic:
                if not has_account_session(directory):
                    raise ValueError("所选账号的本地会话不存在，请点击“选择账号 / 登录”手动登录；不会自动换号")
            else:
                save_selected_account(self.app_data, profile["id"])
        except (OSError, ValueError) as exc:
            if self._workflow_mode:
                self._workflow_fail(str(exc))
            else:
                messagebox.showwarning("信息不完整", str(exc))
            return
        self._clear_login_state()
        self._set_log_group("系统")
        self.log(f"【账号】开始登录：{name}")

        def operation() -> tuple[str, int]:
            from telethon.errors import SessionPasswordNeededError
            try:
                with logged_telegram_client(str(directory / "account"), api_id, api_hash, self.log) as client:
                    client.connect()
                    if not client.is_user_authorized():
                        if automatic:
                            raise RuntimeError("保存的登录状态已失效，请点击“选择账号 / 登录”重新登录此账号")
                        self.log(f"【账号】{name}：会话未登录或已失效，需要验证码")
                        client.send_code_request(phone)
                        code = self.ask_secret("Telegram 验证", "请输入 Telegram 收到的验证码：", masked=True)
                        try:
                            client.sign_in(phone, code)
                        except SessionPasswordNeededError:
                            password = self.ask_secret("两步验证", "请输入 Telegram 两步验证密码：", masked=True)
                            client.sign_in(password=password)
                    user = client.get_me()
                    verify_account_user(profile, user, phone)
                    profile["user_id"] = user.id
                    saver = getattr(getattr(client, "session", None), "save", None)
                    session_value = saver() if callable(saver) else ""
                    if isinstance(session_value, str) and session_value:
                        save_session_string(session_blob_path(directory), session_value)
                    save_saved_credentials(directory / "credentials.bin", api_id, api_hash, phone)
                    save_account_profile(self.app_data, profile)
                    return user.first_name or user.username or str(user.id), user.id
            except Exception as exc:
                # Exception request dumps can contain codes, hashes or phone numbers.
                safe_messages = {
                    "保存的登录状态已失效，请点击“选择账号 / 登录”重新登录此账号",
                    "登录已取消",
                    "当前会话与所选账号不一致，已停止操作，原会话保留",
                    "当前会话与所选手机号不一致，已停止操作，原会话保留",
                    "无法确认当前会话的账号身份，请检查所选账号",
                    SESSION_LOCKED_TEXT,
                    SESSION_BROKEN_TEXT,
                }
                detail = str(exc) if str(exc) in safe_messages else type(exc).__name__
                if isinstance(exc, sqlite3.OperationalError):
                    detail = f"{type(exc).__name__}: {exc}"
                if "Flood" in type(exc).__name__ and isinstance(getattr(exc, "seconds", None), int):
                    detail = f"FLOOD_WAIT，需等待 {exc.seconds} 秒后再尝试"
                raise RuntimeError(f"账号登录未完成：{detail}") from None

        def success(result: tuple[str, int]) -> None:
            self._login_token = ""
            account_name, user_id = result
            self.refresh_account_profiles()
            self.logged_in = True
            self.active_account_id = profile["id"]
            self.active_user_id = user_id
            self.account_status.set(f"已登录：{account_name}")
            self.connection_indicator.configure(style="Online.TLabel")
            self.log(f"【账号】Telegram 登录成功：{name}（{account_name}）")
            window = getattr(self, "login_settings_window", None)
            if window is not None and window.winfo_exists():
                window.destroy()
            if self._workflow_mode:
                self.root.after(0, self._workflow_start_capture)
            elif not automatic:
                messagebox.showinfo("登录成功", f"当前账号：{name}（{account_name}）")

        self._login_token = uuid4().hex
        token = self._login_token
        self.root.after(60000, lambda: self._login_watchdog(token))
        failure_callback = on_failure or (self._workflow_fail if self._workflow_mode else self.open_login_settings)
        self.run_worker(operation, success, on_failure=failure_callback)

    def _login_watchdog(self, token: str) -> None:
        if token != self._login_token:
            return
        self._login_token = ""
        if not self._busy:
            return
        self.log("【账号】登录超时未返回（网络异常），已恢复界面；请重试")
        self.set_busy(False, "登录超时：网络异常，请重试")
        if self._workflow_mode:
            self._workflow_fail("登录超时：网络异常")
        else:
            self.open_login_settings()

    def validate_task(self) -> tuple[Path, Path, date, clock_time, clock_time, str, dict]:
        group_name = self.chat.get().strip()
        group_profile = dict(self.selected_group_profile())
        start_clock = parse_clock(group_profile["start_time"])
        end_clock = parse_clock(group_profile["end_time"])
        notes_file = group_notes_path(self.program_root, group_name).resolve()
        if not notes_file.is_file():
            raise ValueError("该群的同名备注 JSON 不存在，请在设置里重新保存群配置")
        output = Path(self.output_path.get().strip().strip('"')).expanduser().resolve()
        self.settings["output_path"] = str(output)
        _write_group_settings(self.program_root, self.settings)
        try:
            target_day = parse_day(self.day.get())
        except ValueError as exc:
            raise ValueError("日期格式应为 YYYY-MM-DD") from exc
        return notes_file, output, target_day, start_clock, end_clock, group_name, group_profile

    def start_download(self, notes_override: dict[str, str] | None = None, retry: bool = False) -> None:
        if self._busy:
            return
        try:
            (
                notes_file,
                output,
                target_day,
                start_clock,
                end_clock,
                group_name,
                group_profile,
            ) = self.validate_task()
            if notes_override is not None:
                default_notes, address_notes = dict(notes_override), {}
            else:
                default_notes, address_notes = load_group_notes(notes_file)
            chat = group_profile["address"]
            chat_id = group_profile.get("chat_id")
            if chat_id is not None:
                task_account_id = group_profile.get("bound_account_id", "")
                account_profile = next(
                    (dict(item) for item in self.account_profiles if item["id"] == task_account_id), None
                )
                if account_profile is None:
                    raise ValueError("私密群绑定的账号不存在，请在“选择账号 / 登录”中登录该账号后重试")
                task_directory = account_directory(self.app_data, task_account_id)
                saved = load_saved_credentials(task_directory / "credentials.bin")
                if saved is None:
                    raise ValueError("私密群绑定账号的登录配置不存在，请先登录该账号后重试")
                if not has_account_session(task_directory):
                    raise ValueError("私密群绑定账号的本地会话不存在，请先登录该账号后重试")
            else:
                if not self.logged_in or not self.active_account_id or self.active_account_id != self.selected_account_id:
                    if self._workflow_mode:
                        self._workflow_fail("默认账号尚未登录")
                    else:
                        messagebox.showwarning("尚未登录", "请先点击“选择账号 / 登录”")
                    return
                task_account_id = self.active_account_id
                account_profile = {"user_id": self.active_user_id}
                task_directory = account_directory(self.app_data, task_account_id)
                saved = load_saved_credentials(task_directory / "credentials.bin")
                if saved is None:
                    raise ValueError("所选账号的登录配置不存在，原会话文件未改动")
            api_id, api_hash, account_phone = saved
            session_path = task_directory / "account"
        except (OSError, ValueError) as exc:
            if self._workflow_mode:
                self._workflow_fail(str(exc))
            else:
                messagebox.showwarning("信息不完整", str(exc))
            return
        active_account_id = self.active_account_id
        self._set_log_group(group_name)
        if chat_id is not None:
            self.log(f"【账号】本次私密群抓取使用：{account_profile.get('name') or task_account_id}；Chat ID：{chat_id}")

        def operation() -> tuple[int, int, int, int, Path, float]:
            operation_started = time.monotonic()
            group_output = dated_group_output(output, target_day, group_name)
            status_enabled = notes_override is None
            status_path = capture_status_path(self.status_root, group_name) if status_enabled else None
            try:
                previous_status = load_capture_status(status_path) if status_path else None
            except (OSError, ValueError) as exc:
                self.log(f"【状态】读取失败：{status_path}（{type(exc).__name__}）")
                raise
            if status_path:
                self.log(f"【状态】读取：{status_path}；记录日期：{(previous_status or {}).get('date', '无记录')}")
            first_capture = (
                not retry
                or previous_status is None
                or previous_status.get("date") != target_day.isoformat()
                or not group_output.is_dir()
            )
            incremental = bool(status_enabled and retry and not first_capture)
            chats = split_chat_addresses(chat)
            if chat_id is None and not chats:
                raise ValueError("群地址不能为空，请在设置里填写至少一个 Telegram 群地址")
            source_order = [str(chat_id)] if chat_id is not None else list(chats)
            base_notes_by_source = {
                address: address_notes.get(address, default_notes) for address in source_order
            }
            if incremental:
                source_scan_notes = {
                    address: notes_for_retry(notes_map, previous_status)
                    for address, notes_map in base_notes_by_source.items()
                }
            else:
                source_scan_notes = dict(base_notes_by_source)
            all_labels = sorted({
                label for notes_map in base_notes_by_source.values() for label in notes_map.values()
            })
            pending_labels = {
                label for notes_map in source_scan_notes.values() for label in notes_map.values()
            }
            retry_full_day = incremental and any(
                not (previous_status or {}).get("remarks", {}).get(label, {}).get("pending_message_ids")
                for label in pending_labels
            )
            if not retry:
                reason = "点击正常抓取，执行全量"
            elif previous_status is None:
                reason = "无状态记录，执行全量"
            elif previous_status.get("date") != target_day.isoformat():
                reason = "状态日期与抓取日期不同，执行全量"
            elif not group_output.is_dir():
                reason = "对应日期结果文件夹不存在，执行全量"
            else:
                reason = "同日状态与结果文件夹存在，执行增量复抓"
            self.log(f"【复抓】{reason}；抓取日期 {target_day}")
            if incremental and all_labels:
                skipped_labels = set(all_labels) - pending_labels
                self.log(f"【复抓】待检查备注（{len(pending_labels)}）：{'、'.join(sorted(pending_labels)) or '无'}")
                self.log(f"【复抓】已完成跳过（{len(skipped_labels)}）：{'、'.join(sorted(skipped_labels)) or '无'}")
            elif not all_labels:
                self.log("【复抓】备注为空：抓取范围内全部图片" + ("，本次只扫描新增消息" if incremental else ""))
            start_local = datetime.combine(target_day, start_clock, CN_TZ)
            end_local = datetime.combine(target_day, end_clock, CN_TZ) + timedelta(minutes=1)
            start_utc = start_local.astimezone(timezone.utc)
            end_utc = end_local.astimezone(timezone.utc)

            with logged_telegram_client(str(session_path), api_id, api_hash, self.log) as client:
                client.connect()
                if not client.is_user_authorized():
                    if task_account_id == active_account_id:
                        self._post_to_main(self._clear_login_state)
                        raise RuntimeError("登录已失效，请重新登录")
                    raise RuntimeError(
                        f"绑定账号 {account_profile.get('name') or task_account_id} 的登录已失效，请重新登录该账号后重试"
                    )
                try:
                    verify_account_user(account_profile, client.get_me(), account_phone)
                except (RuntimeError, ValueError):
                    if task_account_id == active_account_id:
                        self._post_to_main(self._clear_login_state)
                    raise
                entities = []
                if chat_id is not None:
                    entities.append((str(chat_id), resolve_private_chat(client, chat_id)))
                else:
                    for chat_address in chats:
                        try:
                            entity = client.get_entity(chat_address)
                        except (ValueError, TypeError):
                            wanted = normalized(chat_address).casefold()
                            matches = [
                                d.entity
                                for d in client.iter_dialogs()
                                if normalized(d.name or "").casefold() == wanted
                            ]
                            if len(matches) != 1:
                                raise ValueError("无法唯一找到该群，请改用 @用户名或 t.me 链接")
                            entity = matches[0]
                        entities.append((chat_address, entity))

                self.log(
                    f"【扫描】正在{'增量' if incremental else '全量'}扫描 {len(entities)} 个群（逐个扫描） {target_day} "
                    f"{start_clock.strftime('%H:%M')}～{end_clock.strftime('%H:%M')} 的消息……"
                )
                self.set_progress(0, 0, "扫描中：已发现 0 张")
                all_messages = []
                media_messages = []
                source_messages: dict[str, list] = {address: [] for address in source_order}
                source_media: dict[str, list] = {address: [] for address in source_order}
                seen_keys: set[tuple[str, int]] = set()
                message_sources: dict[int, str] = {}

                def add_message(message, require_new: bool = False, min_id: int = 0) -> None:
                    if message is None:
                        return
                    key = (chat_address, getattr(message, "id", None))
                    if key in seen_keys:
                        self.log(f"【扫描】来源 {chat_address} 消息 {message.id}：重复消息ID，按现有去重规则跳过")
                        return
                    if require_new and message.id <= min_id:
                        return
                    if message.date < start_utc or message.date >= end_utc:
                        return
                    seen_keys.add(key)
                    all_messages.append(message)
                    source_messages[chat_address].append(message)
                    message_sources[id(message)] = chat_address
                    mime = (message.file.mime_type if getattr(message, "file", None) else "") or ""
                    if getattr(message, "photo", None) or mime.startswith("image/"):
                        media_messages.append(message)
                        source_media[chat_address].append(message)
                    if len(all_messages) % 50 == 0:
                        self.set_progress(len(media_messages), 0, f"扫描中：已发现 {len(media_messages)} 张")
                    if len(all_messages) % 200 == 0:
                        self.log(f"【扫描】当前来源 {chat_address}；累计扫描 {len(all_messages)} 条，发现 {len(media_messages)} 张图片")

                last_message_id = int((previous_status or {}).get("last_message_id", 0) or 0)
                saved_last_ids = (previous_status or {}).get("last_message_ids", {})
                if not isinstance(saved_last_ids, dict):
                    saved_last_ids = {}
                current_last_ids: dict[str, int] = {}
                pending_ids = sorted(
                    {
                        int(message_id)
                        for item in (previous_status or {}).get("remarks", {}).values()
                        for message_id in item.get("pending_message_ids", [])
                    }
                )
                for source_index, (chat_address, entity) in enumerate(entities, 1):
                    source_started = time.monotonic()
                    before_messages, before_images = len(all_messages), len(media_messages)
                    source_last_id = int(saved_last_ids.get(chat_address, 0) or 0)
                    self.log(f"【扫描】开始链接 {source_index}/{len(entities)}：{chat_address}")
                    if retry_full_day:
                        self.log(f"【复抓】来源 {chat_address}：未完成备注没有旧消息ID，重新扫描当天消息")
                    elif incremental:
                        self.log(f"【复抓】来源 {chat_address}：读取消息ID > {source_last_id}，重查待处理消息 {len(pending_ids)} 条")
                    source_ids: list[int] = []
                    if retry_full_day:
                        for message in client.iter_messages(entity, offset_date=end_utc):
                            if message.date < start_utc:
                                break
                            add_message(message)
                            if message is not None and (chat_address, getattr(message, "id", None)) in seen_keys:
                                source_ids.append(message.id)
                    elif incremental:
                        if pending_ids:
                            getter = getattr(client, "get_messages", None)
                            pending_messages = getter(entity, ids=pending_ids) if getter else []
                            if pending_messages is None:
                                pending_messages = []
                            elif not isinstance(pending_messages, (list, tuple)):
                                pending_messages = [pending_messages]
                            for message in pending_messages:
                                add_message(message)
                                if message is not None and (chat_address, getattr(message, "id", None)) in seen_keys:
                                    source_ids.append(message.id)
                        try:
                            iterator = client.iter_messages(entity, min_id=source_last_id, offset_date=end_utc)
                        except TypeError:
                            iterator = client.iter_messages(entity, offset_date=end_utc)
                        for message in iterator:
                            if message.date < start_utc:
                                break
                            add_message(message, require_new=True, min_id=source_last_id)
                            if message is not None and (chat_address, getattr(message, "id", None)) in seen_keys:
                                source_ids.append(message.id)
                    else:
                        for message in client.iter_messages(entity, offset_date=end_utc):
                            if message.date < start_utc:
                                break
                            add_message(message)
                            if message is not None and (chat_address, getattr(message, "id", None)) in seen_keys:
                                source_ids.append(message.id)
                    current_last_ids[chat_address] = max([source_last_id, *source_ids], default=source_last_id)
                    self.log(f"【扫描】结束链接 {source_index}/{len(entities)}：{chat_address}；"
                             f"本次纳入 {len(all_messages) - before_messages} 条消息、{len(media_messages) - before_images} 张图片；"
                             f"耗时 {format_duration(time.monotonic() - source_started)}")

                scanned = len(all_messages)
                self.set_progress(len(media_messages), 0, f"备注匹配：共 {len(media_messages)} 张图片")

                source_selections: dict[str, dict[int, set[str]]] = {}
                source_preview_caches: dict[str, dict[int, bytes]] = {}
                source_exact_labels: dict[str, set[str]] = {}
                ignored_note_ids: dict[str, set[int]] = defaultdict(set)

                def prepare_source(chat_address: str) -> tuple[dict[int, set[str]], dict[int, bytes]]:
                    source_media_messages = source_media.get(chat_address, [])
                    source_notes = source_scan_notes.get(chat_address, {})
                    source_base_notes = base_notes_by_source.get(chat_address, {})
                    adjacent_labels = set()
                    visual_adjacent_labels = set()
                    first_image_ocr_labels = set()
                    first_image_detected_labels = None
                    bidirectional_labels = set()
                    exact_labels = set()
                    if group_name == SPECIAL_RETRY_GROUP:
                        adjacent_labels.update(YANRAN_ADJACENT_LABELS.intersection(source_notes.values()))
                        visual_adjacent_labels.update(
                            adjacent_labels - YANRAN_FIRST_IMAGE_OCR_LABELS
                        )
                        first_image_ocr_labels.update(
                            YANRAN_FIRST_IMAGE_OCR_LABELS.intersection(source_notes.values())
                        )
                        bidirectional_labels.update(adjacent_labels)
                    if group_name == HUANGDAXIAN_GROUP:
                        adjacent_labels.update(HUANGDAXIAN_ADJACENT_LABELS.intersection(source_notes.values()))
                        first_image_ocr_labels.update(adjacent_labels)
                        first_image_detected_labels = HUANGDAXIAN_OCR_LABELS.intersection(source_notes.values())
                        bidirectional_labels.update(adjacent_labels)
                        if "战狼" in adjacent_labels:
                            exact_labels.add("战狼")
                    if group_name == MUXI_GROUP:
                        exact_labels.update(MUXI_EXACT_LABELS.intersection(source_notes.values()))
                    marker_filter_labels = set()
                    if group_name == XINAO_EXPERT_GROUP:
                        marker_filter_labels.update(XINAO_EXPERT_FILTER_LABELS.intersection(source_notes.values()))
                    source_exact_labels[chat_address] = set(exact_labels)
                    selection = (
                        {}
                        if incremental and source_base_notes and not source_notes
                        else build_download_selection(
                            source_media_messages,
                            source_notes,
                            special_adjacent_labels=adjacent_labels,
                            bidirectional_adjacent_labels=bidirectional_labels,
                            exact_labels=exact_labels,
                            similarity_labels=visual_adjacent_labels,
                            log=self.log,
                        )
                    )
                    preview_cache: dict[int, bytes] = {}
                    if visual_adjacent_labels or first_image_ocr_labels or marker_filter_labels:
                        groups = ordered_media_groups(source_media_messages)
                        preview_ids: set[int] = set()
                        first_image_preview_ids: set[int] = set()
                        marker_preview_ids: set[int] = set()
                        if visual_adjacent_labels:
                            preview_ids.update(
                                collect_adjacent_preview_messages(
                                    source_media_messages,
                                    selection,
                                    visual_adjacent_labels,
                                    bidirectional_labels,
                                )
                            )
                        if first_image_ocr_labels:
                            first_image_preview_ids = collect_adjacent_first_preview_messages(
                                source_media_messages,
                                selection,
                                first_image_ocr_labels,
                                bidirectional_labels,
                            )
                            preview_ids.update(first_image_preview_ids)
                        if marker_filter_labels:
                            marker_preview_ids = collect_label_group_messages(
                                source_media_messages, selection, marker_filter_labels
                            )
                            preview_ids.update(marker_preview_ids)
                        ocr_engine = None
                        if first_image_preview_ids or marker_preview_ids:
                            self.set_progress(0, 0, "加载 GPU OCR 模型…")
                            ocr_engine = get_ocr_engine()
                            self.log(
                                f"【识别】OCR 配置：GPU-only / {OCR_DEVICE} / FP32 / "
                                f"{OCR_MODEL_DET} + {OCR_MODEL_REC}"
                            )
                        preview_messages = {
                            message.id: message for message in source_media_messages if message.id in preview_ids
                        }
                        preview_started = time.monotonic()
                        preview_done = 0
                        preview_total = len(preview_messages)
                        self.set_progress(0, preview_total, f"预览下载：0 / {preview_total}", phase_started=preview_started)

                        async def load_similarity_previews() -> None:
                            semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

                            async def load_preview(message) -> None:
                                nonlocal preview_done
                                async with semaphore:
                                    try:
                                        payload = await message.download_media(file=bytes)
                                        if isinstance(payload, bytes):
                                            preview_cache[message.id] = payload
                                    except Exception as exc:
                                        self.log(f"【识别】预览下载失败：来源 {message_sources.get(id(message))} 消息 {message.id}（{type(exc).__name__}: {exc}）")
                                    finally:
                                        preview_done += 1
                                        self.set_progress(preview_done, preview_total,
                                                          f"预览下载：{preview_done} / {preview_total}", phase_started=preview_started)

                            await asyncio.gather(*(load_preview(message) for message in preview_messages.values()))

                        def download_previews() -> None:
                            preview_loop = getattr(client, "loop", None)
                            owns_preview_loop = preview_loop is None
                            if owns_preview_loop:
                                preview_loop = asyncio.new_event_loop()
                            try:
                                preview_loop.run_until_complete(load_similarity_previews())
                            finally:
                                if owns_preview_loop:
                                    preview_loop.close()

                        download_previews()

                        def tianji_similarity(anchor, candidate) -> bool:
                            nonlocal preview_messages, preview_started, preview_done, preview_total
                            required = [*anchor, *candidate]
                            preview_messages = {message.id: message for message in required
                                                if message.id not in preview_cache}
                            if preview_messages:
                                preview_started = time.monotonic()
                                preview_done = 0
                                preview_total = len(preview_messages)
                                self.set_progress(0, preview_total, f"相似度补图：0 / {preview_total}",
                                                  phase_started=preview_started)
                                download_previews()
                            missing = [message.id for message in required if not preview_cache.get(message.id)]
                            if missing:
                                raise OcrError(f"天机阁相似度检查图片缺失：{missing}；本次任务已中止")
                            return image_groups_are_similar(
                                [preview_cache[message.id] for message in anchor],
                                [preview_cache[message.id] for message in candidate],
                                min_intersection=TIANJI_MIN_COLOR_INTERSECTION,
                                max_aspect_ratio=TIANJI_MAX_ASPECT_RATIO,
                            )

                        if visual_adjacent_labels or first_image_ocr_labels:
                            self.set_progress(0, 0, f"图片比对：共 {len(groups)} 组")
                            selection = build_download_selection(
                                source_media_messages,
                                source_notes,
                                special_adjacent_labels=adjacent_labels,
                                bidirectional_adjacent_labels=bidirectional_labels,
                                exact_labels=exact_labels,
                                similarity_labels=visual_adjacent_labels,
                                group_similarity=lambda anchor, candidate: image_groups_are_similar(
                                    [preview_cache[message.id] for message in anchor if message.id in preview_cache],
                                    [preview_cache[message.id] for message in candidate if message.id in preview_cache],
                                ),
                                log=self.log,
                            )
                        checked_pairs: set[tuple[int, int]] = set()
                        first_ocr_cache: dict = {}
                        while first_image_ocr_labels and first_image_preview_ids:
                            ocr_started = time.monotonic()
                            first_ocr_total = len(first_image_preview_ids)
                            self.set_progress(
                                0, first_ocr_total,
                                f"文字识别：0 / {first_ocr_total}",
                                phase_started=ocr_started,
                            )
                            selection = add_first_image_ocr_immediate_groups(
                                source_media_messages,
                                selection,
                                first_image_ocr_labels,
                                preview_cache,
                                ocr_engine=ocr_engine,
                                bidirectional_labels=bidirectional_labels,
                                detected_labels=first_image_detected_labels,
                                checked_pairs=checked_pairs,
                                ocr_cache=first_ocr_cache,
                                group_similarity=tianji_similarity if group_name == SPECIAL_RETRY_GROUP else None,
                                log=self.log,
                                on_progress=lambda done, total: self.set_progress(
                                    done, total, f"文字识别：{done} / {total}",
                                    phase_started=ocr_started,
                                ),
                            )
                            if group_name != SPECIAL_RETRY_GROUP:
                                break
                            # Track edges: a rejected group may still match a different adjacent anchor.
                            first_image_preview_ids = collect_adjacent_first_preview_messages(
                                source_media_messages, selection, first_image_ocr_labels, bidirectional_labels,
                                checked_pairs=checked_pairs,
                            )
                            preview_messages = {
                                message.id: message for message in source_media_messages
                                if message.id in first_image_preview_ids and message.id not in preview_cache
                            }
                            if preview_messages:
                                preview_started = time.monotonic()
                                preview_done = 0
                                preview_total = len(preview_messages)
                                self.set_progress(0, preview_total, f"预览下载：0 / {preview_total}",
                                                  phase_started=preview_started)
                                download_previews()
                        if marker_filter_labels and marker_preview_ids:
                            self.set_progress(0, 0, "标题识别：检查不要组特征词…")
                            marker_started = time.monotonic()
                            selection, ignored_ids = filter_unwanted_ocr_groups(
                                source_media_messages,
                                selection,
                                marker_filter_labels,
                                preview_cache,
                                XINAO_EXPERT_UNWANTED_MARKERS,
                                ocr_engine=ocr_engine,
                                log=self.log,
                                on_progress=lambda done, total: self.set_progress(
                                    done, total, f"标题识别：{done} / {total}",
                                    phase_started=marker_started,
                                ),
                            )
                            for label, ids in ignored_ids.items():
                                ignored_note_ids[label].update(ids)
                    return selection, preview_cache

                matched_total = 0
                for source_index, address in enumerate(source_order, 1):
                    self.log(f"【识别】链接 {source_index}/{len(source_order)}：{address}")
                    source_selection, source_cache = prepare_source(address)
                    source_selections[address] = source_selection
                    source_preview_caches[address] = source_cache
                    matched_total += sum(
                        message.id in source_selection for message in source_media.get(address, [])
                    )
                self.set_progress(
                    0,
                    matched_total,
                    f"扫描完成：共发现 {len(media_messages)} 张图片，符合条件 {matched_total} 张",
                )
                self.log(f"【扫描】扫描完成：共发现 {len(media_messages)} 张图片，符合条件 {matched_total} 张。")
                if not incremental and group_output.exists():
                    self.log(f"【状态】全量抓取覆盖本日期结果目录：{group_output}")
                    shutil.rmtree(group_output)
                group_output.mkdir(parents=True, exist_ok=True)
                counts: Counter[str] = Counter()
                downloaded_ids: set[int] = set()
                successful_label_ids: dict[str, set[int]] = defaultdict(set)
                failed_label_ids: dict[str, set[int]] = defaultdict(set)
                processed = 0
                rows = []
                file_outcomes: Counter[str] = Counter()
                download_started = time.monotonic()
                self.log(f"【下载】开始：{matched_total} 张待处理，并发 {DOWNLOAD_CONCURRENCY}；结果目录 {group_output}")
                self.set_progress(0, matched_total, f"正在处理：0 / {matched_total}", phase_started=download_started)

                async def download_selected() -> None:
                    semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

                    async def download_message(message, labels: set[str], preview_cache: dict[int, bytes]) -> None:
                        nonlocal processed
                        async with semaphore:
                            ext = message.file.ext if message.file else ".jpg"
                            if not ext or not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext):
                                ext = ".jpg"
                            stamp = message.date.astimezone(CN_TZ).strftime("%Y%m%d_%H%M%S")
                            filename = f"{stamp}_{message.id}{ext.lower()}"
                            for label in sorted(labels):
                                folder = group_output / safe_folder_name(label)
                                folder.mkdir(parents=True, exist_ok=True)
                                destination = folder / filename
                                success = destination.exists()
                                outcome = "已有跳过" if success else "网络下载"
                                failure = "下载未返回文件"
                                if not success and message.id in preview_cache:
                                    try:
                                        destination.write_bytes(preview_cache[message.id])
                                        success = True
                                        outcome = "预览复用"
                                    except OSError as exc:
                                        self.log(f"【下载】预览保存失败，尝试正常下载：消息 {message.id}（{type(exc).__name__}: {exc}）")
                                if not success:
                                    try:
                                        success = bool(await message.download_media(file=str(destination)))
                                    except Exception as exc:
                                        failure = f"{type(exc).__name__}: {exc}"
                                if not success:
                                    failed_label_ids[label].add(message.id)
                                    file_outcomes["失败"] += 1
                                    self.log(f"【下载】失败：来源 {message_sources.get(id(message))} 备注 {label} 消息 {message.id}（{failure}）")
                                    continue
                                file_outcomes[outcome] += 1
                                successful_label_ids[label].add(message.id)
                                downloaded_ids.add(message.id)
                                counts[label] += 1
                                rows.append(
                                    [label, message.id, message.date.astimezone(CN_TZ).isoformat(), str(destination)]
                                )
                                self.log(f"【下载】{outcome}：来源 {message_sources.get(id(message))}；{label} → {filename}")
                            processed += 1
                            self.set_progress(
                                processed,
                                matched_total,
                                f"正在处理：{processed} / {matched_total}", phase_started=download_started,
                            )

                    for address in source_order:
                        source_selection = source_selections.get(address, {})
                        source_cache = source_preview_caches.get(address, {})
                        await asyncio.gather(
                            *(
                                download_message(message, source_selection[message.id], source_cache)
                                for message in source_media.get(address, [])
                                if message.id in source_selection
                            )
                        )

                download_loop = getattr(client, "loop", None)
                owns_loop = download_loop is None
                if owns_loop:
                    download_loop = asyncio.new_event_loop()
                try:
                    download_loop.run_until_complete(download_selected())
                finally:
                    if owns_loop:
                        download_loop.close()
                    self.log(f"【汇总】文件处理：网络下载 {file_outcomes['网络下载']}，预览复用 {file_outcomes['预览复用']}，"
                             f"已有跳过 {file_outcomes['已有跳过']}，失败 {file_outcomes['失败']}；"
                             f"下载阶段耗时 {format_duration(time.monotonic() - download_started)}（按目标文件计数）")

                self.set_progress(0, 0, "保存结果与抓取状态…")
                report = group_output / "提取报告.csv"
                append_report = incremental and report.is_file() and report.stat().st_size > 0
                with report.open("a" if append_report else "w", newline="", encoding="utf-8-sig") as handle:
                    writer = csv.writer(handle)
                    if not append_report:
                        writer.writerow(["备注", "消息ID", "北京时间", "本地文件"])
                    writer.writerows(rows)
                self.log(f"【状态】提取报告已{'追加' if append_report else '保存'}：{report}")

                labels = list(dict.fromkeys(all_labels))
                note_matches: dict[str, set[int]] = defaultdict(set)
                broad_exact_matches: dict[str, set[int]] = defaultdict(set)
                for address in source_order:
                    source_notes_map = source_scan_notes.get(address, {})
                    source_exact = source_exact_labels.get(address, set())
                    source_all = source_messages.get(address, [])
                    for label, ids in build_note_message_ids(
                        source_all, source_notes_map, exact_labels=source_exact
                    ).items():
                        note_matches[label].update(ids)
                    narrow_notes = {
                        keyword: label for keyword, label in source_notes_map.items() if label in source_exact
                    }
                    for label, ids in build_note_message_ids(source_all, narrow_notes).items():
                        broad_exact_matches[label].update(ids)
                note_matches = dict(note_matches)
                broad_exact_matches = dict(broad_exact_matches)
                for label, ignored_message_ids in ignored_note_ids.items():
                    if label in note_matches:
                        note_matches[label] -= ignored_message_ids
                selected_label_ids: dict[str, set[int]] = defaultdict(set)
                for source_selection in source_selections.values():
                    for message_id, selected_labels in source_selection.items():
                        for label in selected_labels:
                            selected_label_ids[label].add(message_id)
                previous_remarks = (previous_status or {}).get("remarks", {}) if incremental else {}
                remarks = {}
                for label in labels:
                    previous = previous_remarks.get(label, {})
                    previous_image_ids = {int(item) for item in previous.get("image_message_ids", [])}
                    current_image_ids = successful_label_ids.get(label, set())
                    image_ids = previous_image_ids | current_image_ids
                    previous_pending = {int(item) for item in previous.get("pending_message_ids", [])}
                    # Retire only reread captions rejected by the exact rule, not missing
                    # messages or failed album/OCR selections with an empty caption.
                    retired_pending = (
                        previous_pending & broad_exact_matches.get(label, set())
                    ) - note_matches.get(label, set()) - selected_label_ids.get(label, set())
                    if retired_pending:
                        previous_pending -= retired_pending
                        self.log(f"【状态】{label}：已重新核验并移除不符合精准备注规则的旧待抓消息 "
                                 f"{sorted(retired_pending)}；保留未读到和真实失败项")
                    pending_ids = (
                        previous_pending
                        | note_matches.get(label, set())
                        | failed_label_ids.get(label, set())
                    ) - image_ids
                    previous_count = int(previous.get("image_count", 0) or 0)
                    image_count = max(previous_count, len(image_ids))
                    if pending_ids and image_count:
                        status = "部分完成"
                    elif pending_ids:
                        status = "待复抓"
                    elif image_count:
                        status = "已完成"
                    else:
                        status = "未匹配"
                    remarks[label] = {
                        "status": status,
                        "image_count": image_count,
                        "image_message_ids": sorted(image_ids),
                        "pending_message_ids": sorted(pending_ids),
                    }

                if status_enabled:
                    now = datetime.now(CN_TZ).isoformat()
                    all_ids = [message.id for message in all_messages]
                    status_payload = {
                        "group_name": group_name,
                        "date": target_day.isoformat(),
                        "time_range": f"{start_clock.strftime('%H:%M')} ～ {end_clock.strftime('%H:%M')}",
                        "first_capture_time": (
                            previous_status.get("first_capture_time", now)
                            if incremental and previous_status
                            else now
                        ),
                        "last_capture_time": now,
                        "last_message_id": max([last_message_id, *all_ids], default=last_message_id),
                        "last_message_ids": current_last_ids,
                        "remarks": remarks,
                    }
                    try:
                        save_capture_status(status_path, status_payload)
                    except OSError as exc:
                        self.log(f"【状态】抓取状态保存失败：{status_path}（{type(exc).__name__}: {exc}）；本次状态未成功更新")
                        raise
                    self.log(f"【状态】抓取状态已覆盖保存：{status_path}；日期 {target_day}；备注 {len(remarks)} 个")
                    unmatched = [label for label, item in remarks.items() if item["status"] != "已完成"]
                else:
                    unmatched = [label for label in labels if counts[label] == 0]
                (group_output / "未匹配备注.txt").write_text("\n".join(unmatched), encoding="utf-8-sig")
                self.log(f"【汇总】待补抓备注（{len(unmatched)}）：{'、'.join(unmatched) or '无'}")
                return (
                    scanned,
                    len(downloaded_ids),
                    len(unmatched),
                    matched_total,
                    group_output,
                    time.monotonic() - operation_started,
                )

        def success(result: tuple[int, int, int, int, Path, float]) -> None:
            scanned, downloaded, unmatched, matched_total, result_path, total_elapsed = result
            self._stop_capture_progress()
            self.update_first_capture_status()
            self.task_status.set(
                f"完成：{matched_total} / {matched_total}｜总耗时 {format_duration(total_elapsed)}"
            )
            self.log(f"【汇总】完成：扫描 {scanned} 条，成功处理 {downloaded} 张（含已有文件），未完成备注 {unmatched} 条；"
                     f"总耗时 {format_duration(total_elapsed)}")
            if self._workflow_mode:
                self.root.after(0, lambda: self._workflow_finish(0))
            else:
                messagebox.showinfo(
                    "复抓完成" if retry else "提取完成",
                    f"扫描消息：{scanned}\n提取图片：{downloaded}\n未匹配备注：{unmatched}\n\n结果：{result_path}",
                )

        self._start_capture_progress()
        self.run_worker(operation, success)


class GroupSettingsDialog:
    def __init__(self, app: TelegramDownloaderApp):
        self.app = app
        self.selected_name = ""
        self.window = Toplevel(app.root)
        self.window.title("群配置设置")
        self.window.configure(background=DARK_BG)
        self.window.transient(app.root)
        self.window.grab_set()
        self.window.after_idle(lambda: app._enable_dark_title_bar(self.window))

        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="自定义群名称").grid(row=0, column=0, sticky="w", pady=5)
        self.name = StringVar()
        ttk.Entry(frame, textvariable=self.name, font=FORM_FONT).grid(
            row=0, column=1, sticky="ew", pady=5, padx=(10, 0)
        )
        ttk.Label(frame, text="群地址").grid(row=1, column=0, sticky="w", pady=5)
        self.address = StringVar()
        self.address_entry = ttk.Entry(frame, textvariable=self.address, font=FORM_FONT)
        self.address_entry.grid(row=1, column=1, sticky="ew", pady=5, padx=(10, 0))
        ttk.Label(
            frame,
            text="群地址可填写 @用户名、t.me 链接或完整群名；多个群请用 | 分隔。",
            style="Hint.TLabel",
        ).grid(row=2, column=1, sticky="w", pady=(0, 8), padx=(10, 0))
        ttk.Label(frame, text="Chat ID（私密群）").grid(row=3, column=0, sticky="w", pady=5)
        self.chat_id = StringVar()
        ttk.Entry(frame, textvariable=self.chat_id, font=FORM_FONT).grid(
            row=3, column=1, sticky="ew", pady=5, padx=(10, 0)
        )
        ttk.Label(frame, text="绑定账号").grid(row=4, column=0, sticky="w", pady=5)
        self.bound_account = StringVar()
        self.bound_account_combo = ttk.Combobox(
            frame, textvariable=self.bound_account, state="readonly", font=FORM_FONT
        )
        self.bound_account_combo.grid(row=4, column=1, sticky="ew", pady=5, padx=(10, 0))
        ttk.Label(
            frame,
            text="只抓公开群时留空；填写负数 Chat ID 并选择绑定账号后，本配置只抓该 ID，地址保存为空。",
            style="Hint.TLabel",
        ).grid(row=5, column=1, sticky="w", pady=(0, 8), padx=(10, 0))
        self.chat_id.trace_add("write", lambda *_args: self.toggle_private_fields())
        frame.columnconfigure(1, weight=1)

        time_frame = ttk.Frame(frame)
        time_frame.grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.all_day = BooleanVar(value=True)
        ttk.Checkbutton(time_frame, text="全天", variable=self.all_day, command=self.toggle_time_fields).pack(side="left")
        ttk.Label(time_frame, text="开始时间").pack(side="left", padx=(18, 6))
        self.start_time = StringVar(value="00:00")
        self.start_entry = ttk.Entry(time_frame, textvariable=self.start_time, width=8, font=FORM_FONT)
        self.start_entry.pack(side="left")
        ttk.Label(time_frame, text="结束时间").pack(side="left", padx=(18, 6))
        self.end_time = StringVar(value="23:59")
        self.end_entry = ttk.Entry(time_frame, textvariable=self.end_time, width=8, font=FORM_FONT)
        self.end_entry.pack(side="left")

        self.tree = ttk.Treeview(frame, columns=("name", "address", "time"), show="headings", height=10)
        self.tree.heading("name", text="自定义群名称")
        self.tree.heading("address", text="群地址 / Chat ID")
        self.tree.heading("time", text="时间范围")
        self.tree.column("name", width=160)
        self.tree.column("address", width=330)
        self.tree.column("time", width=150)
        self.tree.grid(row=7, column=0, columnspan=2, sticky="nsew", pady=(4, 10))
        self.tree.bind("<<TreeviewSelect>>", self.select_item)
        frame.rowconfigure(7, weight=1)

        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=0, columnspan=2, sticky="ew")
        ttk.Button(buttons, text="新增", command=self.new_item).pack(side="left")
        ttk.Button(buttons, text="保存", command=self.save_item).pack(side="left", padx=8)
        ttk.Button(buttons, text="删除配置", command=self.delete_item).pack(side="left")
        ttk.Button(buttons, text="打开配置文件夹", command=self.open_folder).pack(side="left", padx=8)
        ttk.Button(buttons, text="关闭", command=self.window.destroy).pack(side="right")
        self.toggle_time_fields()
        self.toggle_private_fields()
        self.refresh()
        app._center_dialog(self.window, 900, 720)

    def account_choices(self) -> list[str]:
        return [f"{index + 1}. {profile['name']}" for index, profile in enumerate(self.app.account_profiles)]

    def selected_bound_account_id(self) -> str:
        index = self.bound_account_combo.current()
        profiles = self.app.account_profiles
        if 0 <= index < len(profiles):
            return profiles[index]["id"]
        return ""

    def set_bound_account(self, account_id: str) -> None:
        index = next((i for i, profile in enumerate(self.app.account_profiles) if profile["id"] == account_id), -1)
        if index >= 0:
            self.bound_account_combo.current(index)
        else:
            self.bound_account_combo.set("")
        self.bound_account.set(self.bound_account_combo.get())

    def toggle_private_fields(self) -> None:
        private = bool(self.chat_id.get().strip())
        self.address_entry.configure(state="disabled" if private else "normal")

    def toggle_time_fields(self) -> None:
        if self.all_day.get():
            self.start_time.set("00:00")
            self.end_time.set("23:59")
        state = "disabled" if self.all_day.get() else "normal"
        self.start_entry.configure(state=state)
        self.end_entry.configure(state=state)

    def refresh(self, selected: str = "") -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        self.bound_account_combo.configure(values=self.account_choices())
        selected_id = None
        for item in self.app.settings.get("groups", []):
            time_range = f"{item['start_time']} ～ {item['end_time']}"
            address = str(item["chat_id"]) if item.get("chat_id") is not None else item["address"]
            item_id = self.tree.insert("", "end", values=(item["name"], address, time_range))
            if item["name"] == selected:
                selected_id = item_id
        if selected_id:
            self.tree.selection_set(selected_id)
            self.tree.focus(selected_id)

    def select_item(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        name = self.tree.item(selection[0], "values")[0]
        profile = next(item for item in self.app.settings["groups"] if item["name"] == name)
        self.selected_name = name
        self.name.set(name)
        self.address.set(profile["address"])
        chat_id = profile.get("chat_id")
        self.chat_id.set(str(chat_id) if chat_id is not None else "")
        self.set_bound_account(profile.get("bound_account_id", ""))
        self.start_time.set(profile["start_time"])
        self.end_time.set(profile["end_time"])
        self.all_day.set(profile["start_time"] == "00:00" and profile["end_time"] == "23:59")
        self.toggle_time_fields()
        self.toggle_private_fields()

    def new_item(self) -> None:
        self.selected_name = ""
        self.name.set("")
        self.address.set("")
        self.chat_id.set("")
        self.set_bound_account("")
        self.all_day.set(True)
        self.toggle_time_fields()
        self.toggle_private_fields()

    def save_item(self) -> None:
        try:
            chat_id_text = self.chat_id.get().strip()
            chat_id = None
            if chat_id_text:
                if not re.fullmatch(r"-\d+", chat_id_text):
                    raise ValueError("Chat ID 必须是负数整数，例如 -1004401898428")
                chat_id = int(chat_id_text)
            save_group_profile(
                self.app.program_root,
                self.app.settings,
                self.selected_name,
                self.name.get(),
                self.address.get(),
                "00:00" if self.all_day.get() else self.start_time.get(),
                "23:59" if self.all_day.get() else self.end_time.get(),
                chat_id=chat_id,
                bound_account_id=self.selected_bound_account_id() or None,
            )
        except (OSError, ValueError) as exc:
            messagebox.showwarning("无法保存", str(exc), parent=self.window)
            return
        saved_name = validate_group_name(self.name.get())
        self.selected_name = saved_name
        self.app.reload_group_profiles(saved_name)
        self.refresh(saved_name)
        self.app.log(f"已保存群配置：{saved_name}；备注 JSON：{group_notes_path(self.app.program_root, saved_name)}")

    def delete_item(self) -> None:
        if not self.selected_name:
            messagebox.showwarning("请选择", "请先选择要删除的群配置", parent=self.window)
            return
        if not messagebox.askyesno(
            "删除群配置",
            f"确定删除“{self.selected_name}”的配置吗？\n对应备注 JSON 会保留，不会删除。",
            parent=self.window,
        ):
            return
        deleted = self.selected_name
        delete_group_profile(self.app.program_root, self.app.settings, deleted)
        self.new_item()
        self.app.reload_group_profiles()
        self.refresh()

    def open_folder(self) -> None:
        self.app._open_with_shell(group_directory(self.app.program_root))


def run_self_test() -> None:
    notes = {normalized("测试备注"): "测试备注"}
    class Message:
        def __init__(self, message_id, text, group_id):
            self.id, self.raw_text, self.grouped_id = message_id, text, group_id

    result = build_selection([Message(1, "前缀测试备注后缀", 9), Message(2, "", 9)], notes)
    assert result == {1: {"测试备注"}, 2: {"测试备注"}}


def run_workflow_check(program_root: Path, group_name: str | None = None) -> int:
    """Read-only validation for scheduled single-group runs."""
    try:
        settings = load_group_settings(program_root, read_only=True)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"WORKFLOW_CHECK_FAIL: 群配置不可加载：{exc}", file=sys.stderr)
        return 1
    groups = settings.get("groups", [])
    wanted = groups
    if group_name is not None:
        wanted = [item for item in groups if item.get("name") == group_name.strip()]
        if not wanted:
            print(f"WORKFLOW_CHECK_FAIL: 未找到群配置：{group_name}", file=sys.stderr)
            return 1
    if not wanted:
        print("WORKFLOW_CHECK_FAIL: 没有可用群配置", file=sys.stderr)
        return 1

    errors = []
    group_root = Path(program_root) / GROUP_DIR_NAME
    for profile in wanted:
        name = profile["name"]
        if profile.get("chat_id") is None and not split_chat_addresses(profile.get("address", "")):
            errors.append(f"{name}：群地址为空")
            continue
        notes_path = group_root / f"{validate_group_name(name)}.json"
        if not notes_path.is_file():
            errors.append(f"{name}：备注 JSON 不存在")
            continue
        try:
            load_group_notes(notes_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{name}：备注 JSON 不可加载（{exc}）")
    if errors:
        print("WORKFLOW_CHECK_FAIL: " + "；".join(errors), file=sys.stderr)
        return 1
    print(f"WORKFLOW_CHECK_OK: {len(wanted)} 个群配置可加载")
    return 0


def run_ocr_self_test(output_file: Path) -> bool:
    payload = {
        "ok": False,
        "configured_device": OCR_DEVICE,
        "runtime_device": "",
        "gpu_name": "",
        "paddle_version": "",
        "cuda_version": "",
        "model_det": OCR_MODEL_DET,
        "model_rec": OCR_MODEL_REC,
        "inference_executed": False,
        "smoke_keyword_matched": False,
        "error": "",
    }
    try:
        import paddle

        payload["paddle_version"] = str(getattr(paddle, "__version__", ""))
        payload["cuda_version"] = str(paddle.version.cuda())
        engine = get_ocr_engine()
        payload["runtime_device"] = str(paddle.device.get_device())
        payload["gpu_name"] = str(paddle.device.cuda.get_device_name(0))

        from PIL import ImageDraw, ImageFont

        font_candidates = [
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf",
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeui.ttf",
        ]
        font_path = next((path for path in font_candidates if path.is_file()), None)
        if font_path is None:
            raise RuntimeError("未找到可用的 Windows 测试字体")
        image = Image.new("RGB", (960, 240), "white")
        ImageDraw.Draw(image).text((40, 70), "GPU OCR 12345", fill="black", font=ImageFont.truetype(str(font_path), 64))
        output = io.BytesIO()
        image.save(output, format="PNG")
        matched = ocr_labels_from_payload(output.getvalue(), {"12345"}, ocr_engine=engine)
        payload["inference_executed"] = True
        payload["smoke_keyword_matched"] = "12345" in matched
        if not payload["smoke_keyword_matched"]:
            raise RuntimeError("GPU OCR 自检未识别到测试关键词 12345")
        payload["ok"] = True
    except Exception as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return bool(payload["ok"])


def main(workflow_group: str | None = None, workflow_log: Path | None = None) -> int:
    root = Tk()
    app = TelegramDownloaderApp(root, show_account_dialog=workflow_group is None, workflow_log=workflow_log)
    if workflow_group is not None:
        root.withdraw()
        root.after_idle(lambda: app.run_workflow_group(workflow_group))
    root.mainloop()
    return 0 if workflow_group is None else (app._workflow_exit_code if app._workflow_exit_code is not None else 1)


if __name__ == "__main__":
    if "--ocr-self-test" in sys.argv:
        output_index = sys.argv.index("--ocr-self-test") + 1
        output_file = Path(sys.argv[output_index])
        raise SystemExit(0 if run_ocr_self_test(output_file) else 1)
    elif "--self-test" in sys.argv:
        run_self_test()
    elif "--workflow-check" in sys.argv:
        check_index = sys.argv.index("--workflow-check") + 1
        check_group = (
            sys.argv[check_index]
            if check_index < len(sys.argv) and not sys.argv[check_index].startswith("--")
            else None
        )
        raise SystemExit(run_workflow_check(runtime_root(), check_group))
    elif "--workflow-group" in sys.argv:
        workflow_index = sys.argv.index("--workflow-group") + 1
        if workflow_index >= len(sys.argv) or not sys.argv[workflow_index].strip():
            raise SystemExit("用法：--workflow-group <群名>")
        workflow_log = None
        if "--workflow-log" in sys.argv:
            log_index = sys.argv.index("--workflow-log") + 1
            if log_index >= len(sys.argv) or not sys.argv[log_index].strip():
                raise SystemExit("用法：--workflow-log <日志路径>")
            workflow_log = Path(sys.argv[log_index])
        raise SystemExit(main(sys.argv[workflow_index], workflow_log))
    elif "--workflow-log" in sys.argv:
        raise SystemExit("用法：--workflow-group <群名> --workflow-log <日志路径>")
    else:
        main()
