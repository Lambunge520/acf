# -*- coding: utf-8 -*-
"""
AMD 降压黑屏修复工具

针对 Ryzen Curve Optimizer / PBO 降压、Radeon 显卡节能导致的：
黑屏、花屏、驱动超时（TDR）、从空闲恢复失败等问题。

原理：降压后 CPU/GPU 在空闲、C-State、ULPS、PCIe ASPM、MPO 等低功耗状态
下电压/时钟过低，显示引擎或 Infinity Fabric 掉线。本工具关闭这些节能路径，
让系统停留在稳定电压区间。

注意：若一键修复后仍黑屏，说明 BIOS/Ryzen Master 负压过大，必须减小降压幅度。
本工具不能把不稳定的硬件超频变成稳定，只能阻止 Windows 把电压再往下拉。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import string
import subprocess
import sys
import traceback
import winreg
from datetime import datetime
from typing import Callable, List, Optional, Tuple

import tkinter as tk
from tkinter import messagebox

# ---------------------------------------------------------------------------
# DPI
# ---------------------------------------------------------------------------
# DPI 感知初始化：按优先级依次尝试，命中即停
for _dpi_setup in (
    lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(-4),  # Per-Monitor V2
    lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
    lambda: ctypes.windll.shcore.SetProcessDpiAwareness(1),
    lambda: ctypes.windll.user32.SetProcessDPIAware(),
):
    try:
        _dpi_setup()
        break
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
APP_NAME = "AMD 降压黑屏修复工具"
APP_VERSION = "2.1.0"
MUTEX_NAME = "Global\\AMDBlackScreenFix_Mutex_v2"
ERROR_ALREADY_EXISTS = 183

DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
POWER_SCHEMES = r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"
GRAPHICS_DRIVERS = r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers"
DWM_KEY = r"SOFTWARE\Microsoft\Windows\Dwm"
SESSION_POWER = r"SYSTEM\CurrentControlSet\Control\Session Manager\Power"
CPU_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"

# powercfg GUID
SCHEME_ULTIMATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"
SCHEME_BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
SCHEME_HIGH_PERF = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"

SUB_PROCESSOR = "54533251-82be-4824-96c1-47b60b740d00"
SUB_PCIEXPRESS = "501a4d13-42af-4429-9fd1-a8218c268e20"
SUB_USB = "2a737441-1930-4402-8d77-b2bebba308a3"

SETTING_IDLE_DISABLE = "3b04d4fd-1cc7-4f23-ab1c-d1337819c4bb"   # 1 = 禁用空闲
SETTING_MIN_PROC = "893dee8e-2bef-41e0-89c6-b55d0929964c"       # 百分比
SETTING_MAX_PROC = "bc5038f7-23e0-4960-96da-33abaf5935ec"
SETTING_CPMINCORES = "0cc5b647-c1df-4637-891a-dec35c318583"
SETTING_CPMAXCORES = "ea062031-0e34-4ff1-9b6d-eb1059334028"
SETTING_ASPM = "ee12f906-d277-404b-b6da-e5fa1a576df5"           # 0=关 1=中 2=最大
SETTING_USB_SS = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"         # 0=关 1=开

WOW64_64 = getattr(winreg, "KEY_WOW64_64KEY", 0x0100)
REG_READ = winreg.KEY_READ | WOW64_64
REG_SET = winreg.KEY_READ | winreg.KEY_WRITE | WOW64_64

# 这些项写在「当前电源计划」里，换计划后必须重写，否则看起来勾了其实没带过去
SCHEME_BOUND_KEYS = frozenset({"idle", "minproc", "parking", "aspm", "usbss"})

GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

AC_INDEX_RE = re.compile(
    r"(?:Current AC Power Setting Index|当前交流电源设置索引|目前 AC 電源設定索引)\s*:\s*0x([0-9A-Fa-f]+)",
    re.I,
)
DC_INDEX_RE = re.compile(
    r"(?:Current DC Power Setting Index|当前直流电源设置索引|目前 DC 電源設定索引)\s*:\s*0x([0-9A-Fa-f]+)",
    re.I,
)

# UI
BG = "#1B1218"
CARD = "#2B1A24"
ACCENT = "#E91E8C"
ACCENT_DARK = "#8B0A50"
TEXT = "#FFFFFF"
MUTED = "#C9B4C0"
OK_COLOR = "#3DFF9A"
WARN_COLOR = "#FFC857"
ERR_COLOR = "#FF6B8A"
BTN_BG = "#E91E8C"
BTN_BG2 = "#3D2A36"
TITLE_FONT = ("Microsoft YaHei UI", 16, "bold")
HEAD_FONT = ("Microsoft YaHei UI", 11, "bold")
TEXT_FONT = ("Microsoft YaHei UI", 10)
NOTE_FONT = ("Microsoft YaHei UI", 9)
SMALL_FONT = ("Microsoft YaHei UI", 8)

# ---------------------------------------------------------------------------
# 日志 / 配置
# ---------------------------------------------------------------------------
def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _config_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "AMDBlackScreenFix")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return _app_dir()
    return path


LOG_PATH = os.path.join(_config_dir(), "AMDBlackScreenFix.log")
CONFIG_PATH = os.path.join(_config_dir(), "config.json")

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("amd-fix")


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_config(data: dict) -> None:
    """原子写入配置文件：先写临时文件，再重命名覆盖，避免中途崩溃损坏配置。"""
    tmp_path = CONFIG_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_PATH)
    except OSError as e:
        log.warning("保存配置失败: %s", e)
        try:
            os.remove(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 管理员 / 单实例
# ---------------------------------------------------------------------------
def is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _alert(title: str, msg: str, kind: str = "error") -> None:
    """弹出消息框；若 Tk 初始化失败则降级到 stderr 输出。"""
    try:
        tmp = tk.Tk()
        tmp.withdraw()
        tmp.attributes("-topmost", True)
        try:
            if kind == "error":
                messagebox.showerror(title, msg, parent=tmp)
            elif kind == "warn":
                messagebox.showwarning(title, msg, parent=tmp)
            else:
                messagebox.showinfo(title, msg, parent=tmp)
        finally:
            tmp.destroy()
    except Exception:
        # Tk 不可用（例如远程桌面/无桌面环境），降级到 stderr
        prefix = {"error": "错误", "warn": "警告"}.get(kind, "信息")
        print(f"[{prefix}] {title}: {msg}", file=sys.stderr)


def run_as_admin() -> None:
    if sys.platform != "win32":
        _alert("不支持", "本工具仅支持 Windows。")
        sys.exit(1)
    try:
        if getattr(sys, "frozen", False):
            executable = sys.executable
            params = subprocess.list2cmdline(sys.argv[1:])
        else:
            executable = sys.executable
            script = os.path.abspath(sys.argv[0])
            params = subprocess.list2cmdline([script] + sys.argv[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
        if rc <= 32:
            _alert("需要管理员权限", "无法获取管理员权限（可能取消了 UAC）。\n修改电源计划和显卡注册表必须用管理员运行。")
    except Exception as e:
        _alert("提权失败", f"无法获取管理员权限：{e}")
    sys.exit(0)


def already_running() -> bool:
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            return False
        exists = kernel32.GetLastError() == ERROR_ALREADY_EXISTS
        # 关闭句柄避免泄漏（内核仍持有互斥量，进程退出时自动释放）
        kernel32.CloseHandle(handle)
        return exists
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
def reg_get(root, path: str, name: str, access: int = REG_READ):
    try:
        with winreg.OpenKey(root, path, 0, access) as key:
            value, _typ = winreg.QueryValueEx(key, name)
            return value
    except OSError:
        return None


def reg_set_dword(root, path: str, name: str, value: int) -> bool:
    """写入 DWORD 值，自动校验范围并防止符号溢出。"""
    try:
        val = int(value)
    except (TypeError, ValueError):
        log.error("写入注册表失败 %s\\%s: 值 %r 无法转换为整数", path, name, value)
        return False
    # DWORD 为无符号 32 位，Python int 可能超出范围
    if val < 0 or val > 0xFFFFFFFF:
        log.error("写入注册表失败 %s\\%s: 值 %d 超出 DWORD 范围 (0-4294967295)", path, name, val)
        return False
    try:
        try:
            key = winreg.OpenKey(root, path, 0, REG_SET)
        except FileNotFoundError:
            key = winreg.CreateKeyEx(root, path, 0, REG_SET)
        with key:
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, val)
        return True
    except OSError as e:
        log.error("写入注册表失败 %s\\%s: %s", path, name, e)
        return False


def reg_delete_value(root, path: str, name: str) -> bool:
    try:
        with winreg.OpenKey(root, path, 0, REG_SET) as key:
            winreg.DeleteValue(key, name)
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        log.warning("删除注册表值失败 %s\\%s: %s", path, name, e)
        return False


def _enum_subkeys(path: str) -> List[str]:
    names = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, REG_READ) as key:
            i = 0
            while True:
                try:
                    names.append(winreg.EnumKey(key, i))
                    i += 1
                except OSError:
                    break
    except OSError:
        pass
    return names


# ---------------------------------------------------------------------------
# 硬件识别
# ---------------------------------------------------------------------------
def get_cpu_name() -> str:
    value = reg_get(winreg.HKEY_LOCAL_MACHINE, CPU_KEY, "ProcessorNameString")
    if isinstance(value, str) and value.strip():
        return re.sub(r"\s+", " ", value).strip()
    return "未知 CPU"


def is_amd_cpu(name: Optional[str] = None) -> bool:
    n = (name or get_cpu_name()).lower()
    return "amd" in n or "ryzen" in n or "threadripper" in n or "epyc" in n


def iter_display_adapters() -> List[dict]:
    """返回物理显卡适配器信息（跳过非 0000-9999 的驱动类键）。"""
    adapters = []
    for sub in _enum_subkeys(DISPLAY_CLASS):
        if not re.fullmatch(r"\d{4}", sub):
            continue
        path = f"{DISPLAY_CLASS}\\{sub}"
        desc = reg_get(winreg.HKEY_LOCAL_MACHINE, path, "DriverDesc") or ""
        provider = reg_get(winreg.HKEY_LOCAL_MACHINE, path, "ProviderName") or ""
        device = reg_get(winreg.HKEY_LOCAL_MACHINE, path, "MatchingDeviceId") or ""
        if not desc and not device:
            continue
        desc_s = str(desc)
        provider_s = str(provider)
        device_s = str(device).upper()
        blob = f"{desc_s} {provider_s} {device_s}".lower()
        is_amd = (
            "ven_1002" in device_s.lower()
            or "amd" in blob
            or "ati " in blob
            or "radeon" in blob
            or "advanced micro devices" in blob
        )
        is_basic = "basic display" in blob or "microsoft basic" in blob
        adapters.append(
            {
                "id": sub,
                "path": path,
                "desc": desc_s,
                "provider": provider_s,
                "device": device_s,
                "is_amd": is_amd and not is_basic,
            }
        )
    return adapters


def amd_gpus() -> List[dict]:
    return [a for a in iter_display_adapters() if a["is_amd"]]


def gpu_summary() -> str:
    ads = iter_display_adapters()
    if not ads:
        return "未检测到显卡"
    names = []
    for a in ads:
        label = a["desc"] or a["id"]
        if a["is_amd"]:
            label += " [AMD]"
        names.append(label)
    return " / ".join(names)


# ---------------------------------------------------------------------------
# powercfg
# ---------------------------------------------------------------------------
def _decode(data: bytes) -> str:
    if not data:
        return ""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "cp1252", "mbcs", "cp936"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def run_powercfg(args: List[str]) -> Tuple[int, str, str]:
    """执行 powercfg 命令。参数经过白名单校验，拒绝包含 shell 元字符的输入。"""
    # 安全校验：powercfg 参数只应包含 GUID、数字、斜杠、下划线和字母
    _allowed = set(string.ascii_letters + string.digits + "-{}/ _:")
    for a in args:
        if not all(c in _allowed for c in a):
            log.error("powercfg 参数被拒绝（含非法字符）: %r", a)
            return 1, "", f"参数包含非法字符: {a}"
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            ["powercfg"] + args,
            capture_output=True,
            check=False,
            creationflags=creationflags,
        )
        out = _decode(result.stdout)
        err = _decode(result.stderr).strip()
        if result.returncode != 0:
            log.warning("powercfg %s -> %s %s", args, result.returncode, err)
        return result.returncode, out, err
    except Exception as e:
        log.exception("执行 powercfg 失败")
        return 1, "", str(e)


def get_active_scheme_guid() -> Optional[str]:
    value = reg_get(winreg.HKEY_LOCAL_MACHINE, POWER_SCHEMES, "ActivePowerScheme")
    if isinstance(value, str) and GUID_RE.fullmatch(value):
        return value.lower()
    code, out, _ = run_powercfg(["/getactivescheme"])
    if code == 0 and out:
        m = GUID_RE.search(out)
        if m:
            return m.group(0).lower()
    return None


def list_schemes() -> List[Tuple[str, str]]:
    """[(guid, name), ...]"""
    code, out, _ = run_powercfg(["/list"])
    schemes = []
    if code != 0 or not out:
        return schemes
    for line in out.splitlines():
        m = GUID_RE.search(line)
        if not m:
            continue
        guid = m.group(0).lower()
        name_m = re.search(r"\((.+)\)\s*$", line)
        name = name_m.group(1).strip() if name_m else guid
        schemes.append((guid, name))
    return schemes


def scheme_name(guid: Optional[str]) -> str:
    if not guid:
        return "未知"
    for g, n in list_schemes():
        if g == guid.lower():
            return n
    return guid


def is_ultimate_scheme(guid: Optional[str], name: Optional[str] = None) -> bool:
    """当电源计划是「卓越性能」时返回 True。

    同时匹配 GUID（微软原版）和名称（任何名为「卓越性能」的计划），
    因为用户的系统上可能已经存在 GUID 不同但功能等价的「卓越性能」计划。
    """
    if not guid:
        return False
    if guid.lower() == SCHEME_ULTIMATE:
        return True
    if name is not None:
        return "卓越性能" in name
    return "卓越性能" in scheme_name(guid)


def find_ultimate_guid() -> Optional[str]:
    for guid, name in list_schemes():
        if is_ultimate_scheme(guid, name):
            return guid
    if _scheme_exists(SCHEME_ULTIMATE):
        return SCHEME_ULTIMATE
    return None


def _has_subgroup(scheme_guid: str, subgroup: str) -> bool:
    """检查指定电源计划是否包含某个子组。"""
    code, out, _ = run_powercfg(["/query", scheme_guid, subgroup])
    return code == 0 and bool(out)


def _delete_scheme(guid: str) -> bool:
    """删除电源计划。仅允许删除非内置计划。"""
    builtin = {SCHEME_ULTIMATE, SCHEME_BALANCED, SCHEME_HIGH_PERF}
    if guid.lower() in {g.lower() for g in builtin}:
        return False
    code, _, _ = run_powercfg(["/delete", guid])
    return code == 0


def _rename_scheme(guid: str, new_name: str) -> bool:
    """重命名电源计划。"""
    code, _, _ = run_powercfg(["/changename", guid, new_name])
    return code == 0


def _ensure_complete_ultimate() -> Optional[str]:
    """确保系统中存在一个包含 PCI Express 子组的「卓越性能」计划。

    处理三种情况：
    1. 微软原版卓越性能 GUID 存在且完整 → 直接返回
    2. 有其他名为「卓越性能」的计划但缺少 PCI Express → 删除后从平衡计划重建
    3. 完全没有卓越性能计划 → 从平衡计划复制并重命名
    """
    # 先看微软原版
    if _scheme_exists(SCHEME_ULTIMATE) and _has_subgroup(SCHEME_ULTIMATE, SUB_PCIEXPRESS):
        return SCHEME_ULTIMATE

    # 扫描现有计划，找名为「卓越性能」的
    for guid, name in list_schemes():
        if "卓越性能" in (name or ""):
            if _has_subgroup(guid, SUB_PCIEXPRESS):
                return guid
            # 缺少 PCI Express，删掉重建
            log.warning("计划 %s 缺少 PCI Express 子组，将重建", guid)
            if is_active_scheme(guid):
                set_active_scheme(SCHEME_BALANCED)
            _delete_scheme(guid)
            break

    # 从平衡计划复制
    template = SCHEME_BALANCED if _scheme_exists(SCHEME_BALANCED) else SCHEME_HIGH_PERF
    new_guid, err = duplicate_scheme(template)
    if not new_guid:
        log.error("复制电源计划失败: %s", err)
        return None
    _rename_scheme(new_guid, "卓越性能")
    log.info("已从 %s 复制并重命名为「卓越性能」: %s", template, new_guid)
    return new_guid


def _scheme_exists(guid: str) -> bool:
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, f"{POWER_SCHEMES}\\{guid}", 0, REG_READ
        ):
            return True
    except OSError:
        pass
    code, out, _ = run_powercfg(["/list"])
    return code == 0 and guid.lower() in (out or "").lower()


def duplicate_scheme(template_guid: str) -> Tuple[Optional[str], str]:
    code, out, err = run_powercfg(["-duplicatescheme", template_guid])
    if code == 0 and out:
        m = GUID_RE.search(out)
        if m:
            new_guid = m.group(0).lower()
            if new_guid != template_guid.lower():
                return new_guid, ""
            # 有的系统 duplicatescheme 只打印模板 GUID，再扫一遍列表
            # 返回模板 GUID（该系统 duplicatescheme 不产生新 GUID）
            return new_guid, ""
    return None, err or out.strip() or "复制电源计划失败"


def set_active_scheme(guid: str) -> Tuple[bool, str]:
    code, _out, err = run_powercfg(["/setactive", guid])
    if code == 0:
        return True, ""
    return False, err or "切换电源计划失败"


def read_power_setting(
    subgroup: str, setting: str, scheme: Optional[str] = None
) -> Optional[Tuple[int, int]]:
    scheme = scheme or get_active_scheme_guid()
    if not scheme:
        return None
    path = f"{POWER_SCHEMES}\\{scheme}\\{subgroup}\\{setting}"
    ac = reg_get(winreg.HKEY_LOCAL_MACHINE, path, "ACSettingIndex")
    dc = reg_get(winreg.HKEY_LOCAL_MACHINE, path, "DCSettingIndex")
    if isinstance(ac, int):
        if not isinstance(dc, int):
            dc = ac
        return int(ac), int(dc)

    code, out, _ = run_powercfg(["/query", scheme, subgroup, setting])
    if code != 0 or not out:
        return None
    return _parse_ac_dc(out)


def _parse_ac_dc(out: str) -> Optional[Tuple[int, int]]:
    """只解析“当前 AC/DC 索引”，绝不能把最小/最大可能值当成当前值。"""
    ac_m = AC_INDEX_RE.search(out)
    dc_m = DC_INDEX_RE.search(out)
    if ac_m and dc_m:
        return int(ac_m.group(1), 16), int(dc_m.group(1), 16)
    if ac_m:
        v = int(ac_m.group(1), 16)
        return v, v
    found: List[int] = []
    for line in out.splitlines():
        if not re.search(r"(Current|当前|目前).*(Index|索引)", line, re.I):
            continue
        m = re.search(r"0x([0-9A-Fa-f]+)", line)
        if m:
            found.append(int(m.group(1), 16))
    if len(found) >= 2:
        return found[0], found[1]
    if len(found) == 1:
        return found[0], found[0]
    return None


def write_power_setting(
    subgroup: str,
    setting: str,
    ac_value: int,
    dc_value: int,
    commit: bool = True,
) -> bool:
    scheme = get_active_scheme_guid()
    target = scheme or "SCHEME_CURRENT"
    # 预检：子组是否存在于目标计划中
    if not _has_subgroup(target, subgroup):
        log.warning("目标计划 %s 中不存在子组 %s，跳过写入", target, subgroup)
        return False
    c1, _, e1 = run_powercfg(["/setacvalueindex", target, subgroup, setting, str(ac_value)])
    c2, _, e2 = run_powercfg(["/setdcvalueindex", target, subgroup, setting, str(dc_value)])
    # 如果用具体 GUID 失败且不是 SCHEME_CURRENT，回退到 SCHEME_CURRENT 重试
    if (c1 != 0 or c2 != 0) and target != "SCHEME_CURRENT":
        log.warning("写入 %s 失败，尝试 SCHEME_CURRENT 回退", setting)
        c1, _, e1 = run_powercfg(["/setacvalueindex", "SCHEME_CURRENT", subgroup, setting, str(ac_value)])
        c2, _, e2 = run_powercfg(["/setdcvalueindex", "SCHEME_CURRENT", subgroup, setting, str(dc_value)])
    if c1 != 0 or c2 != 0:
        log.error("写入电源设置失败 %s: %s %s", setting, e1, e2)
        return False
    if commit:
        return commit_power_scheme()
    return True


def commit_power_scheme() -> bool:
    guid = get_active_scheme_guid()
    args = ["/setactive", guid] if guid else ["/setactive", "SCHEME_CURRENT"]
    code, _, err = run_powercfg(args)
    if code != 0:
        log.error("应用电源计划失败: %s", err)
        return False
    return True


def unhide_power_settings() -> None:
    pairs = [
        (SUB_PROCESSOR, SETTING_IDLE_DISABLE),
        (SUB_PROCESSOR, SETTING_MIN_PROC),
        (SUB_PROCESSOR, SETTING_CPMINCORES),
        (SUB_PROCESSOR, SETTING_CPMAXCORES),
        (SUB_PCIEXPRESS, SETTING_ASPM),
        (SUB_USB, SETTING_USB_SS),
    ]
    for sub, setting in pairs:
        run_powercfg(["-attributes", sub, setting, "-ATTRIB_HIDE"])


# ---------------------------------------------------------------------------
# 各项设置：True = 防护已开启（即“黑屏相关节能已关掉”）
# ---------------------------------------------------------------------------
def _both(values: Optional[Tuple[int, int]], pred) -> bool:
    if not values:
        return False
    return pred(values[0]) and pred(values[1])


def get_cpu_idle_disabled() -> bool:
    return _both(read_power_setting(SUB_PROCESSOR, SETTING_IDLE_DISABLE), lambda v: v == 1)


def set_cpu_idle_disabled(disabled: bool) -> bool:
    v = 1 if disabled else 0
    return write_power_setting(SUB_PROCESSOR, SETTING_IDLE_DISABLE, v, v)


def get_min_proc_100() -> bool:
    return _both(read_power_setting(SUB_PROCESSOR, SETTING_MIN_PROC), lambda v: v == 100)


def set_min_proc_100(enable: bool) -> bool:
    if enable:
        ok1 = write_power_setting(SUB_PROCESSOR, SETTING_MIN_PROC, 100, 100, commit=False)
        ok2 = write_power_setting(SUB_PROCESSOR, SETTING_MAX_PROC, 100, 100, commit=True)
        return ok1 and ok2
    return write_power_setting(SUB_PROCESSOR, SETTING_MIN_PROC, 5, 5)


def get_core_parking_off() -> bool:
    return _both(read_power_setting(SUB_PROCESSOR, SETTING_CPMINCORES), lambda v: v == 100)


def set_core_parking_off(enable: bool) -> bool:
    if enable:
        ok1 = write_power_setting(SUB_PROCESSOR, SETTING_CPMINCORES, 100, 100, commit=False)
        ok2 = write_power_setting(SUB_PROCESSOR, SETTING_CPMAXCORES, 100, 100, commit=True)
        return ok1 and ok2
    # 恢复到 Windows 默认的核心停驻值（100% = 不停驻 = 所有核心活跃）
    # 注意：此处 disable 意为"恢复常规行为"，Windows 默认 min=100% 表示不停驻
    return write_power_setting(SUB_PROCESSOR, SETTING_CPMINCORES, 100, 100)


def get_aspm_off() -> bool:
    return _both(read_power_setting(SUB_PCIEXPRESS, SETTING_ASPM), lambda v: v == 0)


def set_aspm_off(enable: bool) -> bool:
    v = 0 if enable else 1
    return write_power_setting(SUB_PCIEXPRESS, SETTING_ASPM, v, v)


def get_usb_ss_off() -> bool:
    return _both(read_power_setting(SUB_USB, SETTING_USB_SS), lambda v: v == 0)


def set_usb_ss_off(enable: bool) -> bool:
    v = 0 if enable else 1
    return write_power_setting(SUB_USB, SETTING_USB_SS, v, v)


def _amd_dword_all(name: str, expected: int) -> Optional[bool]:
    gpus = amd_gpus()
    if not gpus:
        return None
    for g in gpus:
        val = reg_get(winreg.HKEY_LOCAL_MACHINE, g["path"], name)
        if val != expected:
            return False
    return True


def _amd_set_dwords(pairs: List[Tuple[str, int]]) -> bool:
    gpus = amd_gpus()
    if not gpus:
        return False
    ok = True
    for g in gpus:
        for name, value in pairs:
            if not reg_set_dword(winreg.HKEY_LOCAL_MACHINE, g["path"], name, value):
                ok = False
    return ok


def _amd_delete_names(names: List[str]) -> bool:
    gpus = amd_gpus()
    if not gpus:
        return False
    ok = True
    for g in gpus:
        for name in names:
            if not reg_delete_value(winreg.HKEY_LOCAL_MACHINE, g["path"], name):
                ok = False
    return ok


def get_ulps_disabled() -> Optional[bool]:
    """True = 已禁用 ULPS。键不存在时 AMD 默认是开启，视为未禁用。"""
    gpus = amd_gpus()
    if not gpus:
        return None
    for g in gpus:
        val = reg_get(winreg.HKEY_LOCAL_MACHINE, g["path"], "EnableUlps")
        if val != 0:
            return False
    return True


def set_ulps_disabled(disabled: bool) -> bool:
    gpus = amd_gpus()
    if not gpus:
        return False
    value = 0 if disabled else 1
    ok = True
    for g in gpus:
        if not reg_set_dword(winreg.HKEY_LOCAL_MACHINE, g["path"], "EnableUlps", value):
            ok = False
        # EnableUlps_NA=0 让控制面板里的选项可写且不被驱动覆盖回默认
        if not reg_set_dword(winreg.HKEY_LOCAL_MACHINE, g["path"], "EnableUlps_NA", 0):
            ok = False
    return ok


GPU_SLEEP_ON = [
    ("PP_SclkDeepSleepDisable", 1),
    ("PP_MclkDeepSleepDisable", 1),
    ("DisableDrmdmaPowerGating", 1),
    ("DisablePowerGating", 1),
]
GPU_SLEEP_NAMES = [n for n, _ in GPU_SLEEP_ON]


def get_gpu_deepsleep_disabled() -> Optional[bool]:
    gpus = amd_gpus()
    if not gpus:
        return None
    for g in gpus:
        for name, expected in GPU_SLEEP_ON:
            if reg_get(winreg.HKEY_LOCAL_MACHINE, g["path"], name) != expected:
                return False
    return True


def set_gpu_deepsleep_disabled(disabled: bool) -> bool:
    if disabled:
        return _amd_set_dwords(GPU_SLEEP_ON)
    return _amd_delete_names(GPU_SLEEP_NAMES)


def get_mpo_disabled() -> bool:
    return reg_get(winreg.HKEY_LOCAL_MACHINE, DWM_KEY, "OverlayTestMode") == 5


def set_mpo_disabled(disabled: bool) -> bool:
    if disabled:
        return reg_set_dword(winreg.HKEY_LOCAL_MACHINE, DWM_KEY, "OverlayTestMode", 5)
    return reg_delete_value(winreg.HKEY_LOCAL_MACHINE, DWM_KEY, "OverlayTestMode")


def get_tdr_protected() -> bool:
    delay = reg_get(winreg.HKEY_LOCAL_MACHINE, GRAPHICS_DRIVERS, "TdrDelay")
    hags = reg_get(winreg.HKEY_LOCAL_MACHINE, GRAPHICS_DRIVERS, "HwSchMode")
    try:
        delay_ok = delay is not None and int(delay) >= 10
    except (TypeError, ValueError):
        delay_ok = False
    hags_off = hags == 1  # 1=关闭硬件加速 GPU 调度
    return bool(delay_ok and hags_off)


def set_tdr_protected(enable: bool) -> bool:
    if enable:
        ok1 = reg_set_dword(winreg.HKEY_LOCAL_MACHINE, GRAPHICS_DRIVERS, "TdrDelay", 10)
        ok2 = reg_set_dword(winreg.HKEY_LOCAL_MACHINE, GRAPHICS_DRIVERS, "HwSchMode", 1)
        return ok1 and ok2
    ok1 = reg_delete_value(winreg.HKEY_LOCAL_MACHINE, GRAPHICS_DRIVERS, "TdrDelay")
    ok2 = reg_delete_value(winreg.HKEY_LOCAL_MACHINE, GRAPHICS_DRIVERS, "HwSchMode")
    return ok1 and ok2


def get_fast_startup_off() -> bool:
    val = reg_get(winreg.HKEY_LOCAL_MACHINE, SESSION_POWER, "HiberbootEnabled")
    if isinstance(val, int):
        return val == 0
    if isinstance(val, str):
        return val.strip() == "0"
    return False


def set_fast_startup_off(enable: bool) -> bool:
    # enable=True 表示“关闭快速启动”这一防护
    return reg_set_dword(
        winreg.HKEY_LOCAL_MACHINE, SESSION_POWER, "HiberbootEnabled", 0 if enable else 1
    )


def enable_ultimate_performance(save_previous: bool = True) -> Tuple[bool, str]:
    current = get_active_scheme_guid()
    if current and is_ultimate_scheme(current):
        return True, "已处于卓越性能计划"

    cfg = load_config()
    if save_previous and current and not is_ultimate_scheme(current):
        cfg["previous_scheme_guid"] = current
        cfg["previous_scheme_name"] = scheme_name(current)
        save_config(cfg)

    target = _ensure_complete_ultimate()
    if not target:
        # 最后手段：尝试高性能
        hp = SCHEME_HIGH_PERF if _scheme_exists(SCHEME_HIGH_PERF) else None
        if not hp:
            hp, _ = duplicate_scheme(SCHEME_HIGH_PERF)
        if hp:
            ok, e2 = set_active_scheme(hp)
            if ok:
                cfg["used_high_perf_fallback"] = True
                save_config(cfg)
                return True, "卓越性能不可用，已改用高性能"
        return False, "无法创建卓越性能计划"

    ok, err = set_active_scheme(target)
    if not ok:
        return False, err
    return True, "已切换到卓越性能"


def disable_ultimate_performance() -> Tuple[bool, str]:
    cfg = load_config()
    target = cfg.get("previous_scheme_guid")
    if not target or not _scheme_exists(str(target)) or is_ultimate_scheme(str(target)):
        target = SCHEME_BALANCED if _scheme_exists(SCHEME_BALANCED) else None
        if not target:
            new_guid, err = duplicate_scheme(SCHEME_BALANCED)
            if not new_guid:
                return False, err or "找不到可恢复的电源计划"
            target = new_guid
    ok, err = set_active_scheme(str(target))
    if not ok:
        return False, err
    return True, "已恢复电源计划"


def reboot_now() -> None:
    if not is_admin():
        log.error("重启被拒绝：当前没有管理员权限")
        try:
            _alert("需要管理员权限", "没有管理员权限，无法重启电脑。", kind="error")
        except Exception:
            print("错误：没有管理员权限，无法重启电脑。", file=sys.stderr)
        return
    log.info("执行立即重启")
    subprocess.run(
        ["shutdown", "/r", "/t", "0"],
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        check=False,
    )


# ---------------------------------------------------------------------------
# 设置表
# ---------------------------------------------------------------------------
class Setting:
    def __init__(
        self,
        key: str,
        title: str,
        hint: str,
        getter: Callable[[], Optional[bool]],
        setter: Callable[[bool], bool],
        needs_reboot: bool = False,
        group: str = "",
        requires_amd_gpu: bool = False,
    ):
        self.key = key
        self.title = title
        self.hint = hint
        self.getter = getter
        self.setter = setter
        self.needs_reboot = needs_reboot
        self.group = group
        self.requires_amd_gpu = requires_amd_gpu


def build_settings() -> List[Setting]:
    return [
        Setting(
            "ultimate",
            "启用卓越性能电源计划",
            "避免平衡计划在低负载时把 CPU 拉到过低电压/频率。",
            lambda: is_ultimate_scheme(get_active_scheme_guid()),
            lambda on: (enable_ultimate_performance()[0] if on else disable_ultimate_performance()[0]),
            group="CPU / 电源",
        ),
        Setting(
            "idle",
            "禁用 CPU 空闲状态 (C-States)",
            "降压后进入 C-State 最容易黑屏/死机，关闭后 CPU 不再深度休眠。",
            get_cpu_idle_disabled,
            set_cpu_idle_disabled,
            group="CPU / 电源",
        ),
        Setting(
            "minproc",
            "最低处理器状态设为 100%",
            "禁止降频。配合 C-States 关闭，是 Ryzen 降压黑屏的核心对策。",
            get_min_proc_100,
            set_min_proc_100,
            group="CPU / 电源",
        ),
        Setting(
            "parking",
            "禁用核心停驻 (Core Parking)",
            "防止 Windows 停用部分核心，造成瞬时电压/调度不稳。",
            get_core_parking_off,
            set_core_parking_off,
            group="CPU / 电源",
        ),
        Setting(
            "ulps",
            "禁用 ULPS（显卡超低功耗）",
            "Radeon 空闲掉入 ULPS 是经典黑屏原因。注册表里没有该项时会自动创建。需重启。",
            get_ulps_disabled,
            set_ulps_disabled,
            needs_reboot=True,
            group="显卡 / 显示",
            requires_amd_gpu=True,
        ),
        Setting(
            "gpusleep",
            "禁用 GPU 深度休眠与电源门控",
            "关闭 SCLK/MCLK Deep Sleep 和 DRM DMA Power Gating，避免显卡睡死。需重启。",
            get_gpu_deepsleep_disabled,
            set_gpu_deepsleep_disabled,
            needs_reboot=True,
            group="显卡 / 显示",
            requires_amd_gpu=True,
        ),
        Setting(
            "aspm",
            "禁用 PCIe ASPM",
            "PCIe 链路节能会在降压后掉链，表现为瞬间黑屏或驱动重置。",
            get_aspm_off,
            set_aspm_off,
            group="显卡 / 显示",
        ),
        Setting(
            "mpo",
            "禁用 MPO 多平面叠加",
            "Windows 多平面叠加与 AMD 驱动冲突，是 Win10/11 黑屏、闪烁的常见元凶。需重启。",
            get_mpo_disabled,
            set_mpo_disabled,
            needs_reboot=True,
            group="显卡 / 显示",
        ),
        Setting(
            "tdr",
            "延长 TDR 并关闭硬件加速 GPU 调度",
            "把驱动超时从 2 秒提到 10 秒，并关闭 HAGS，减少“显卡驱动停止响应”。需重启。",
            get_tdr_protected,
            set_tdr_protected,
            needs_reboot=True,
            group="显卡 / 显示",
        ),
        Setting(
            "faststart",
            "关闭 Windows 快速启动",
            "混合关机常把错误的电源状态带进下次开机，表现为开机黑屏。需重启。",
            get_fast_startup_off,
            set_fast_startup_off,
            needs_reboot=True,
            group="系统",
        ),
        Setting(
            "usbss",
            "禁用 USB 选择性暂停",
            "避免 USB 设备唤醒时把已经不稳的 PCIe/CPU 电源状态再打乱。",
            get_usb_ss_off,
            set_usb_ss_off,
            group="系统",
        ),
    ]


# ---------------------------------------------------------------------------
# GUI 辅助
# ---------------------------------------------------------------------------
class Tooltip:
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _e=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)
        lbl = tk.Label(
            tw,
            text=self.text,
            justify=tk.LEFT,
            background="#3A2432",
            foreground=TEXT,
            relief=tk.SOLID,
            borderwidth=1,
            font=NOTE_FONT,
            padx=8,
            pady=6,
            wraplength=360,
        )
        lbl.pack()

    def _hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Scrollable(tk.Frame):
    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.vsb = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.inner.bind("<Enter>", lambda e: self._bind_wheel(True))
        self.canvas.bind("<Enter>", lambda e: self._bind_wheel(True))
        self.inner.bind("<Leave>", lambda e: self._bind_wheel(False))
        self.canvas.bind("<Leave>", lambda e: self._bind_wheel(False))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def _bind_wheel(self, on: bool):
        if on:
            self.canvas.bind_all("<MouseWheel>", self._wheel)
        else:
            self.canvas.unbind_all("<MouseWheel>")

    def _wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------
class AMDStabilityApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME}  v{APP_VERSION}")
        self.root.config(bg=BG)
        self.root.minsize(640, 560)
        self._updating = False
        self.reboot_required = False
        self.settings = build_settings()
        self.vars: dict[str, tk.BooleanVar] = {}
        self.status_labels: dict[str, tk.Label] = {}
        self.checks: dict[str, tk.Checkbutton] = {}
        self.has_amd_gpu = bool(amd_gpus())

        self._build()
        self.root.after(50, self._post_init)

    def _post_init(self):
        try:
            unhide_power_settings()
        except Exception:
            log.exception("取消电源设置隐藏属性失败")
        self.refresh()
        self.center_window()

    def _build(self):
        outer = tk.Frame(self.root, bg=BG, padx=22, pady=16)
        outer.pack(fill=tk.BOTH, expand=True)

        tk.Label(outer, text=APP_NAME, font=TITLE_FONT, bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(
            outer,
            text="关闭 Ryzen/Radeon 降压后会把电压再往下拉的系统节能项，从根源减少黑屏。",
            font=NOTE_FONT,
            bg=BG,
            fg=MUTED,
            wraplength=700,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 10))

        info = tk.Frame(outer, bg=CARD, padx=14, pady=10)
        info.pack(fill=tk.X)
        self.cpu_lbl = tk.Label(info, text="", font=TEXT_FONT, bg=CARD, fg=TEXT, anchor="w")
        self.cpu_lbl.pack(fill=tk.X)
        self.gpu_lbl = tk.Label(info, text="", font=TEXT_FONT, bg=CARD, fg=TEXT, anchor="w")
        self.gpu_lbl.pack(fill=tk.X)
        self.plan_lbl = tk.Label(info, text="", font=TEXT_FONT, bg=CARD, fg=TEXT, anchor="w")
        self.plan_lbl.pack(fill=tk.X)

        btns = tk.Frame(outer, bg=BG)
        btns.pack(fill=tk.X, pady=14)
        self._mk_btn(btns, "一键修复黑屏", self.apply_all, BTN_BG).pack(side=tk.LEFT, padx=(0, 8))
        self._mk_btn(btns, "全部恢复默认", self.restore_all, BTN_BG2).pack(side=tk.LEFT, padx=(0, 8))
        self._mk_btn(btns, "刷新状态", self.refresh, BTN_BG2).pack(side=tk.LEFT, padx=(0, 8))
        self._mk_btn(btns, "立即重启", self.ask_reboot, "#6B1A3A").pack(side=tk.RIGHT)

        scroll = Scrollable(outer, bg=BG)
        scroll.pack(fill=tk.BOTH, expand=True)
        body = scroll.inner

        current_group = None
        row = 0
        for s in self.settings:
            if s.group != current_group:
                current_group = s.group
                tk.Label(
                    body, text=current_group, font=HEAD_FONT, bg=BG, fg=ACCENT, anchor="w"
                ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 4), padx=4)
                row += 1

            var = tk.BooleanVar(value=False)
            self.vars[s.key] = var
            star = " *" if s.needs_reboot else ""
            cb = tk.Checkbutton(
                body,
                text=s.title + star,
                variable=var,
                command=lambda k=s.key: self.on_toggle(k),
                bg=BG,
                fg=TEXT,
                activebackground=BG,
                activeforeground=TEXT,
                selectcolor=ACCENT_DARK,
                font=TEXT_FONT,
                cursor="hand2",
                anchor="w",
                justify=tk.LEFT,
                wraplength=460,
            )
            cb.grid(row=row, column=0, sticky="w", padx=8, pady=2)
            self.checks[s.key] = cb
            Tooltip(cb, s.hint)

            st = tk.Label(body, text="检测中", font=TEXT_FONT, bg=BG, fg=MUTED, width=10, anchor="w")
            st.grid(row=row, column=1, sticky="w", padx=8)
            self.status_labels[s.key] = st
            row += 1

            hint = tk.Label(
                body, text=s.hint, font=SMALL_FONT, bg=BG, fg=MUTED, wraplength=620, justify=tk.LEFT, anchor="w"
            )
            hint.grid(row=row, column=0, columnspan=2, sticky="w", padx=28, pady=(0, 6))
            row += 1

        body.columnconfigure(0, weight=1)

        foot = tk.Frame(outer, bg=BG)
        foot.pack(fill=tk.X, pady=(8, 0))
        self.foot_lbl = tk.Label(
            foot,
            text="* 带星号的项必须重启后才完全生效。若修复后仍黑屏，请减小 BIOS / Ryzen Master 的负压。",
            font=NOTE_FONT,
            bg=BG,
            fg=MUTED,
            wraplength=700,
            justify=tk.LEFT,
            anchor="w",
        )
        self.foot_lbl.pack(fill=tk.X)
        self.bar = tk.Label(foot, text="就绪", font=NOTE_FONT, bg=BG, fg=OK_COLOR, anchor="w")
        self.bar.pack(fill=tk.X, pady=(4, 0))

        self.root.bind("<F5>", lambda e: self.refresh())

    def _mk_btn(self, parent, text, cmd, bg) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=cmd,
            font=HEAD_FONT,
            bg=bg,
            fg=TEXT,
            activebackground=ACCENT,
            activeforeground=TEXT,
            relief=tk.FLAT,
            cursor="hand2",
            padx=14,
            pady=6,
            bd=0,
        )

    def center_window(self):
        self.root.update_idletasks()
        w = max(self.root.winfo_reqwidth(), 700)
        h = min(max(self.root.winfo_reqheight(), 720), int(self.root.winfo_screenheight() * 0.9))
        x = (self.root.winfo_screenwidth() - w) // 2
        y = max(0, (self.root.winfo_screenheight() - h) // 3)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def set_busy(self, busy: bool, msg: str = ""):
        self.root.config(cursor="watch" if busy else "")
        if msg:
            self.bar.config(text=msg, fg=WARN_COLOR if busy else OK_COLOR)
        self.root.update_idletasks()

    def on_toggle(self, key: str):
        if self._updating:
            return
        setting = next(s for s in self.settings if s.key == key)
        want = self.vars[key].get()
        if setting.requires_amd_gpu and not self.has_amd_gpu:
            self._updating = True
            self.vars[key].set(False)
            self._updating = False
            messagebox.showwarning("没有 AMD 显卡", "当前没有检测到 AMD/Radeon 显卡，此项无法应用。", parent=self.root)
            self.refresh()
            return
        self.set_busy(True, f"正在应用：{setting.title} …")
        try:
            ok = bool(setting.setter(want))
        except Exception as e:
            log.exception("切换 %s 失败", key)
            ok = False
            messagebox.showerror("错误", f"{setting.title} 失败：{e}", parent=self.root)
        if ok and key == "ultimate":
            # 电源计划变了，C-States / 最低频率等必须写到新计划上
            self._reapply_scheme_bound()
        self.set_busy(False)
        if ok:
            if setting.needs_reboot:
                self.reboot_required = True
            actual = setting.getter()
            if actual is False and want:
                messagebox.showwarning(
                    "未完全生效",
                    f"已写入「{setting.title}」，但读回状态尚未确认。\n请点刷新；若仍未生效，请重启后再看。",
                    parent=self.root,
                )
            elif setting.needs_reboot:
                self.bar.config(text=f"已写入「{setting.title}」，重启后完全生效。", fg=WARN_COLOR)
            else:
                self.bar.config(text=f"已应用「{setting.title}」。", fg=OK_COLOR)
        else:
            messagebox.showerror("失败", f"无法修改「{setting.title}」。请查看日志：\n{LOG_PATH}", parent=self.root)
        self.refresh()

    def _reapply_scheme_bound(self) -> None:
        """把仍勾选的计划内设置写到当前电源计划（换计划后必须做）。

        只重写已勾选（防护开）的项。未勾选的绝不能写成“恢复默认”，
        否则会把卓越性能自带的最低处理器 100% 改回 5%。
        """
        for s in self.settings:
            if s.key not in SCHEME_BOUND_KEYS:
                continue
            if not bool(self.vars[s.key].get()):
                continue
            try:
                s.setter(True)
            except Exception:
                log.exception("重写计划内设置失败: %s", s.key)

    def _apply_one(self, setting: Setting, want: bool) -> bool:
        if setting.requires_amd_gpu and not self.has_amd_gpu:
            return True  # 跳过，不算失败
        try:
            ok = bool(setting.setter(want))
            if ok and setting.needs_reboot and want:
                self.reboot_required = True
            return ok
        except Exception:
            log.exception("应用 %s 失败", setting.key)
            return False

    def apply_all(self):
        if not messagebox.askyesno(
            "一键修复黑屏",
            "将开启全部稳定性防护：\n"
            "· 卓越性能电源计划\n"
            "· 关闭 C-States / 核心停驻，最低处理器 100%\n"
            "· 关闭 ULPS、GPU 深度休眠、PCIe ASPM、MPO\n"
            "· 延长 TDR、关闭 HAGS 与快速启动\n\n"
            "带 * 的项必须重启后才完全生效。\n"
            "功耗和发热会升高，这是为了电压不再掉下去。\n\n是否继续？",
            parent=self.root,
        ):
            return
        self.set_busy(True, "正在一键修复…")
        failed = []
        # 先切电源计划，再写该计划下的 CPU/PCIe 项
        ordered = sorted(self.settings, key=lambda s: 0 if s.key == "ultimate" else 1)
        for s in ordered:
            self.bar.config(text=f"正在应用：{s.title} …")
            self.root.update_idletasks()
            if not self._apply_one(s, True):
                failed.append(s.title)
        self.set_busy(False)
        self.refresh()
        if failed:
            messagebox.showwarning(
                "部分失败",
                "以下项目未能应用：\n- " + "\n- ".join(failed) + f"\n\n日志：{LOG_PATH}",
                parent=self.root,
            )
        else:
            msg = "全部防护已写入。"
            if self.reboot_required:
                msg += "\n\n必须重启电脑后，ULPS / MPO / 快速启动等才会真正生效。"
                if messagebox.askyesno("成功", msg + "\n\n现在重启吗？", parent=self.root):
                    reboot_now()
                    return
            else:
                messagebox.showinfo("成功", msg, parent=self.root)
        self._update_footer()

    def restore_all(self):
        if not messagebox.askyesno(
            "恢复默认",
            "将关闭本工具写入的防护，并尝试恢复到之前的电源计划（或平衡计划）。\n"
            "显卡相关注册表项会删除/还原为驱动默认值。\n\n是否继续？",
            parent=self.root,
        ):
            return
        self.set_busy(True, "正在恢复默认…")
        failed = []
        # 先还原全局注册表和当前计划内的项，再切走卓越性能，
        # 最后把默认值再写一遍到恢复后的计划（设置是按计划存储的）。
        ordered = sorted(self.settings, key=lambda s: 1 if s.key == "ultimate" else 0)
        for s in ordered:
            self.bar.config(text=f"正在恢复：{s.title} …")
            self.root.update_idletasks()
            if not self._apply_one(s, False):
                failed.append(s.title)
        for s in self.settings:
            if s.key in SCHEME_BOUND_KEYS:
                if not self._apply_one(s, False) and s.title not in failed:
                    failed.append(s.title)
        self.set_busy(False)
        self.refresh()
        if failed:
            messagebox.showwarning(
                "部分失败",
                "以下项目未能恢复：\n- " + "\n- ".join(failed),
                parent=self.root,
            )
        else:
            extra = "\n建议重启一次，让显卡驱动重新加载默认节能项。" if self.reboot_required else ""
            messagebox.showinfo("已恢复", "已恢复为常规默认设置。" + extra, parent=self.root)

    def ask_reboot(self):
        if messagebox.askyesno("立即重启", "确定立即重启电脑吗？未保存的工作将丢失。", parent=self.root):
            reboot_now()

    def refresh(self, _e=None):
        self._updating = True
        try:
            self.has_amd_gpu = bool(amd_gpus())
            cpu = get_cpu_name()
            amd_cpu = is_amd_cpu(cpu)
            self.cpu_lbl.config(
                text=f"CPU：{cpu}" + ("  ✓ AMD" if amd_cpu else "  ⚠ 非 AMD，CPU 项仍可改，但本工具主要为 Ryzen 设计")
            )
            self.gpu_lbl.config(text=f"GPU：{gpu_summary()}")
            guid = get_active_scheme_guid()
            self.plan_lbl.config(text=f"当前电源计划：{scheme_name(guid)}")
            # 如果计划名是 GUID（scheme_name 未找到友好名），尝试从注册表读取
            display_name = scheme_name(guid)
            if display_name == guid and guid:
                # scheme_name 只查 powercfg /list，但注册表可能有友好名
                schemes_path = r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"
                try:
                    reg_name = reg_get(winreg.HKEY_LOCAL_MACHINE, f"{schemes_path}\\{guid}", "FriendlyName")
                    if reg_name and isinstance(reg_name, str) and reg_name.strip():
                        display_name = reg_name.strip()
                except Exception:
                    pass
            self.plan_lbl.config(text=f"当前电源计划：{display_name}")

            if not amd_cpu and not self.has_amd_gpu:
                self.bar.config(text="未检测到 AMD CPU 或 AMD 显卡，请确认这是要修的机器。", fg=WARN_COLOR)

            active = 0
            total = 0
            for s in self.settings:
                na = s.requires_amd_gpu and not self.has_amd_gpu
                if na:
                    self.checks[s.key].config(state=tk.DISABLED, fg=MUTED)
                    self.vars[s.key].set(False)
                    self.status_labels[s.key].config(text="无 AMD 显卡", fg=MUTED)
                    continue
                self.checks[s.key].config(state=tk.NORMAL, fg=TEXT)
                total += 1
                try:
                    state = s.getter()
                except Exception:
                    log.exception("读取 %s 失败", s.key)
                    state = None
                on = bool(state)
                self.vars[s.key].set(on)
                if on:
                    active += 1
                    self.status_labels[s.key].config(text="已开启防护", fg=OK_COLOR)
                elif state is None:
                    self.status_labels[s.key].config(text="无法检测", fg=WARN_COLOR)
                else:
                    self.status_labels[s.key].config(text="未开启", fg=ERR_COLOR)
            self._update_footer(active, total)
        finally:
            self._updating = False

    def _update_footer(self, active: Optional[int] = None, total: Optional[int] = None):
        reboot = "  · 有设置尚未重启生效" if self.reboot_required else ""
        if active is None:
            self.bar.config(text="就绪" + reboot, fg=WARN_COLOR if self.reboot_required else OK_COLOR)
        else:
            self.bar.config(
                text=f"防护 {active}/{total} 项已开启{reboot}    日志 {LOG_PATH}",
                fg=OK_COLOR if active == total and total else WARN_COLOR,
            )


def print_status() -> None:
    print(f"{APP_NAME} v{APP_VERSION}")
    print(f"CPU: {get_cpu_name()}")
    print(f"GPU: {gpu_summary()}")
    guid = get_active_scheme_guid()
    print(f"电源计划: {scheme_name(guid)} ({guid})")
    print("-" * 48)
    for s in build_settings():
        try:
            st = s.getter()
        except Exception as e:
            st = f"错误: {e}"
        flag = {True: "ON ", False: "OFF", None: "N/A"}.get(st, str(st))
        print(f"[{flag}] {s.title}")


def main() -> None:
    if sys.platform != "win32":
        print("仅支持 Windows")
        sys.exit(1)

    args = [a.lower() for a in sys.argv[1:]]
    if "--help" in args or "-h" in args:
        print(f"{APP_NAME} v{APP_VERSION}\n  (无参数) 打开图形界面\n  --status   打印当前防护状态\n")
        return

    if not is_admin():
        run_as_admin()
        return

    if "--status" in args or "-s" in args:
        print_status()
        return

    if already_running():
        _alert("已在运行", "工具已经打开了一个窗口。", kind="info")
        return

    log.info("启动 %s v%s", APP_NAME, APP_VERSION)
    root = tk.Tk()
    try:
        AMDStabilityApp(root)
        root.mainloop()
    except Exception:
        log.exception("GUI 异常")
        messagebox.showerror("异常", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
