#!/usr/bin/env python3
#
# 折叠屏测试 - 宿主机折叠控制 HTTP 服务（支持 Mac/Windows）
#
# 功能：监听 HTTP 请求，收到折叠指令后执行 Emulator 命令切换折叠状态。
#       测试代码内的 triggerFold() 通过 HTTP 直接调用本服务，同步等待结果。
#
# 用法：
#   python3 scripts/emulator-control-server.py
#   python3 scripts/emulator-control-server.py "Pura X"   # 指定实例名
#
#   保持运行，测试代码的 triggerFold('half-open') 会请求本服务。
#
# API：
#   GET /fold?state=half-open        切换折叠状态
#   GET /rotation?direction=left     旋转屏幕
#   GET /health                      健康检查
#

import http.server
import json
import subprocess
import sys
import os
import socket
import platform
import threading
import time
import signal
import config as _CFG  # 同目录 config.py（入库默认配置）

# ============ 配置加载 ============
# 优先级（高 -> 低）：命令行参数 > 环境变量 > config.py > 代码默认值


def _pick(env_key, cfg_name, default):
    """统一优先级：环境变量 > config.py 的同名属性 > default。
    env_key 为 None 表示该项不支持环境变量。"""
    if env_key:
        env_val = os.environ.get(env_key)
        if env_val is not None and env_val != "":
            return env_val
    cfg_val = getattr(_CFG, cfg_name, None)
    if cfg_val is not None and cfg_val != "":
        return cfg_val
    return default


# ---- 各配置项（命令行参数在 main() 里覆盖 EMULATOR_INSTANCE）----
# 环境变量 > config.py > 默认值；端口/路径类只支持 config.py 与默认值
EMULATOR_INSTANCE = _pick("EMULATOR_INSTANCE", "EMULATOR_INSTANCE", "Mate X7")
# HEADLESS：环境变量是字符串("0"/"1")，config.py 是布尔；统一转 bool
_headless_raw = _pick("FOLD_HEADLESS", "HEADLESS", False)
if isinstance(_headless_raw, str):
    HEADLESS = _headless_raw.lower() in ("1", "true", "yes")
else:
    HEADLESS = bool(_headless_raw)
EMU_START_TIMEOUT = int(_pick("FOLD_EMU_TIMEOUT", "EMU_START_TIMEOUT", 120))
EMU_POLL_INTERVAL = 2  # 轮询设备上线间隔秒数（固定，不开放配置）

PORT = getattr(_CFG, "PORT", 8766)
DEVICE_PORT = getattr(_CFG, "DEVICE_PORT", 8765)
EMULATOR_PATH_OVERRIDE = _pick("EMULATOR_PATH", "EMULATOR_PATH", "")
HDC_PATH_OVERRIDE = _pick("HDC_PATH", "HDC_PATH", "")
CONNECT_KEY_CFG = _pick("HDC_CONNECT_KEY", "HDC_CONNECT_KEY", None)

# 运行时确定的当前目标设备 connect-key（多设备时用于 hdc -t 路由）
# None 表示尚未确定 / 单设备时 hdc 无需 -t
CURRENT_CONNECT_KEY = None

def find_deveco_root():
    """自动探测 DevEco Studio 安装根目录（不写死路径）"""
    candidates = []

    # 1. 环境变量（最可靠）
    for env_var in ["DEVECO_SDK_HOME", "DEVECO_HOME", "HOS_SDK_HOME"]:
        val = os.environ.get(env_var, "")
        if val:
            candidates.append(val)
            # DEVECO_SDK_HOME 通常指向 sdk 目录，父目录可能是 DevEco 根
            candidates.append(os.path.dirname(val))

    if platform.system() == "Windows":
        # 2. Windows 常见安装位置（扫描盘符 + Program Files）
        for drive in ["C", "D", "E"]:
            candidates.extend([
                f"{drive}:\\Program Files\\Huawei\\DevEco Studio",
                f"{drive}:\\Program Files (x86)\\Huawei\\DevEco Studio",
            ])
        # 3. 用户目录、环境变量 USERPROFILE
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            candidates.append(os.path.join(userprofile, "AppData", "Local", "Huawei", "DevEco Studio"))

        # 4. 从 PATH 找 deveco/studio 相关
        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        for d in path_dirs:
            if "deveco" in d.lower() or "huawei" in d.lower():
                # PATH 里的可能是 bin/子目录，往上找根
                candidates.append(os.path.dirname(os.path.dirname(d)))
                candidates.append(os.path.dirname(d))
    else:
        # Mac
        candidates.extend([
            "/Applications/DevEco-Studio.app/Contents",
            os.path.join(os.environ.get("HOME", ""), "Applications/DevEco-Studio.app/Contents"),
        ])

    # 验证候选，找到包含 emulator 或 sdk 的有效根目录
    for c in candidates:
        if not c or not os.path.isdir(c):
            continue
        # 验证：DevEco 根目录下应有 tools/emulator 或 sdk
        if os.path.isdir(os.path.join(c, "tools", "emulator")) or \
           os.path.isdir(os.path.join(c, "sdk")) or \
           os.path.isdir(os.path.join(c, "Contents")):
            return c
    return None


def find_emulator():
    """自动探测 emulator 路径（不写死）"""
    # emulator 二进制名
    exe_name = "emulator.exe" if platform.system() == "Windows" else "Emulator"

    # 候选路径（最前面优先：环境变量/config.py 覆盖 > DevEco 根目录 > PATH）
    candidates = []

    # 1. 环境变量 EMULATOR_PATH / config.py EMULATOR_PATH 直接指定（最优先）
    if EMULATOR_PATH_OVERRIDE:
        candidates.append(EMULATOR_PATH_OVERRIDE)

    # 2. 从 DevEco 根目录找
    deveco_root = find_deveco_root()
    if deveco_root:
        candidates.extend([
            os.path.join(deveco_root, "tools", "emulator", exe_name),
            os.path.join(deveco_root, "Contents", "tools", "emulator", exe_name),
            os.path.join(deveco_root, "sdk", "tools", "emulator", exe_name),
        ])

    # 3. PATH 里找
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    for d in path_dirs:
        candidates.append(os.path.join(d, exe_name))

    # 验证
    for c in candidates:
        if c and os.path.isfile(c):
            return c

    print(f"  [!] 找不到 emulator，请设置 EMULATOR_PATH 环境变量")
    return exe_name  # 兜底，让命令失败时报错


def find_hdc():
    """自动探测 hdc 路径（不写死）"""
    exe_name = "hdc.exe" if platform.system() == "Windows" else "hdc"

    candidates = []

    # 1. 环境变量 HDC_PATH / config.py HDC_PATH 直接指定（最优先）
    if HDC_PATH_OVERRIDE:
        candidates.append(HDC_PATH_OVERRIDE)
        candidates.append(os.path.join(HDC_PATH_OVERRIDE, exe_name))

    # 2. 从 DevEco 根目录找（sdk/default/openharmony/toolchains/hdc）
    deveco_root = find_deveco_root()
    if deveco_root:
        candidates.extend([
            os.path.join(deveco_root, "sdk", "default", "openharmony", "toolchains", exe_name),
            os.path.join(deveco_root, "Contents", "sdk", "default", "openharmony", "toolchains", exe_name),
            os.path.join(deveco_root, "sdk", "openharmony", "toolchains", exe_name),
        ])

    # 3. PATH 里找
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    for d in path_dirs:
        candidates.append(os.path.join(d, exe_name))

    # 验证
    for c in candidates:
        if c and os.path.isfile(c):
            return c

    return exe_name  # 兜底：假设在 PATH 里

EMULATOR = find_emulator()
HDC = find_hdc()
# EMULATOR_INSTANCE 已在配置加载区（第 82 行）由 _pick 统一设置，
# 此处不再重复赋值，避免覆盖 config.py 的值。

# 启动时打印探测到的路径（方便排查）
def print_paths():
    print(f"  路径探测:")
    print(f"    DevEco 根目录: {find_deveco_root() or '未找到（用环境变量 DEVECO_SDK_HOME 指定）'}")
    print(f"    Emulator: {EMULATOR}{'  [OK]' if os.path.isfile(EMULATOR) else '  [X] 未找到'}")
    print(f"    hdc: {HDC}{'  [OK]' if os.path.isfile(HDC) else '  （用 PATH 兜底）'}")


# ============ 跨平台命令执行辅助 ============

def run_cmd(args, timeout=10):
    """执行命令，跨平台处理 Windows shell 引号。返回 (returncode, combined_output)。"""
    try:
        if platform.system() == "Windows" and isinstance(args, str):
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, shell=True)
        elif isinstance(args, list):
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        else:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, shell=True)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return -1, "命令不存在"
    except subprocess.TimeoutExpired:
        return -1, "命令超时"
    except Exception as e:
        return -1, str(e)


def hdc_cmd(extra_args, timeout=10):
    """执行 hdc 命令，自动在多设备时插入 -t <connect-key> 路由。
    extra_args: hdc 子命令参数列表，如 ['fport', 'ls'] 或 ['rport', 'tcp:8765', 'tcp:8766']。"""
    args = [HDC]
    # 多设备场景：全局加 -t 指定目标，避免 "need connect-key" 错误
    if CURRENT_CONNECT_KEY:
        args += ["-t", CURRENT_CONNECT_KEY]
    if isinstance(extra_args, str):
        extra_args = extra_args.split()
    args += extra_args
    if platform.system() == "Windows":
        # Windows 下 HDC 路径可能含空格，用 shell + 引号
        quoted = " ".join(f'"{a}"' if " " in a else a for a in args)
        return run_cmd(quoted, timeout=timeout)
    return run_cmd(args, timeout=timeout)


# ============ 模拟器实例管理（自动启动 + 状态探测）============

def list_instances():
    """读取 Emulator -list -details，返回实例信息列表。
    每个元素: {'name', 'isRunning'(bool), 'deviceType'}。失败返回空列表。"""
    rc, out = run_cmd([EMULATOR, "-list", "-details"], timeout=10)
    if rc != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    result = []
    for inst in data:
        name = inst.get("name", "").strip()
        if not name:
            continue
        result.append({
            "name": name,
            "isRunning": str(inst.get("isRunning", "")).lower() == "true",
            "deviceType": inst.get("deviceType", ""),
        })
    return result


def list_instance_names():
    """轻量列出所有实例名（Emulator -list），用于提示用户。失败返回空列表。"""
    rc, out = run_cmd([EMULATOR, "-list"], timeout=10)
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def find_instance_status(name):
    """查单个实例状态。返回实例 dict（含 isRunning），找不到返回 None。"""
    for inst in list_instances():
        if inst["name"] == name:
            return inst
    return None


def start_emulator(name):
    """以 detached 后台进程启动指定实例（无窗口模式），不阻塞调用方。
    返回 (success, message)。"""
    args = [EMULATOR, "-start", name]
    if HEADLESS:
        args.append("-noWindow")
    try:
        if platform.system() == "Windows":
            # Windows: detached 需 CREATE_NEW_PROCESS_GROUP（Python 自动处理 detached=True）
            subprocess.Popen(
                args, creationflags=subprocess.DETACHED_PROCESS if hasattr(subprocess, "DETACHED_PROCESS") else 0,
                close_fds=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            # Mac/Linux: 用 start_new_session 脱离父进程，输出丢弃（emulator 自写日志）
            subprocess.Popen(args, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, f"已发出启动命令: {' '.join(args)}"
    except FileNotFoundError:
        return False, f"找不到 Emulator: {EMULATOR}"
    except Exception as e:
        return False, str(e)


def list_targets():
    """hdc list targets，返回在线设备的 connect-key 列表。
    [Empty] 或无输出视为没有设备。"""
    rc, out = run_cmd([HDC, "list", "targets"], timeout=5)
    if rc != 0:
        return []
    keys = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line in ("[Empty]", "Empty", "No target."):
            continue
        # 多设备时每行一个 connect-key，可能含状态后缀，取第一列
        key = line.split()[0]
        if key:
            keys.append(key)
    return keys


def build_instance_connectkey_map():
    """建立「Emulator 实例名 -> hdc connect-key」的映射。
    原理：每个 `Emulator -start <实例名>` 进程会独占监听一个 TCP 端口（如 127.0.0.1:5555），
    该端口就是它在 hdc 里的 connect-key。所以：
      Emulator 进程命令行(-start 实例名) -> PID -> lsof 查监听端口 -> connect-key
    返回 dict: {实例名: '127.0.0.1:5555', ...}。找不到返回空 dict。
    跨平台：Mac/Linux 用 lsof，Windows 用 netstat。"""
    mapping = {}
    is_win = platform.system() == "Windows"

    # 1. 找所有 Emulator -start 进程，提取 (PID, 实例名)
    #    命令行形如：.../Emulator -start <实例名> [-noWindow]
    #    实例名可能含空格（如 "Mate X7"），-start 后到下一个 -flag 之间的 token 都属实例名
    emulator_pids = {}
    try:
        if is_win:
            rc, out = run_cmd(
                'wmic process where "name=\'emulator.exe\'" get ProcessId,CommandLine /format:csv',
                timeout=5)
            for line in out.splitlines():
                if "-start" not in line:
                    continue
                parts = line.split(",")
                # CSV: ...,CommandLine,ProcessId
                if len(parts) < 2:
                    continue
                pid = parts[-1].strip()
                cmdline = ",".join(parts[:-1])
                name = _extract_instance_from_cmdline(cmdline)
                if name and pid:
                    emulator_pids[pid] = name
        else:
            rc, out = run_cmd(["pgrep", "-f", "Emulator -start"], timeout=5)
            for pid in out.split():
                pid = pid.strip()
                if not pid:
                    continue
                rc2, cmdline = run_cmd(["ps", "-p", pid, "-o", "command="], timeout=3)
                name = _extract_instance_from_cmdline(cmdline)
                if name:
                    emulator_pids[pid] = name
    except Exception:
        pass

    if not emulator_pids:
        return mapping

    # 2. 对每个 PID，查它监听的 TCP 端口 -> 组成 connect-key
    for pid, inst_name in emulator_pids.items():
        port = _listening_port(pid)
        if port:
            mapping[inst_name] = f"127.0.0.1:{port}"
    return mapping


def _extract_instance_from_cmdline(cmdline):
    """从命令行提取 -start 后的实例名（支持带空格的名字，如 'Mate X7'）。
    规则：取 '-start' 之后、下一个以 '-' 开头的 flag 之前的所有 token，拼成实例名。"""
    toks = cmdline.split()
    if "-start" not in toks:
        return None
    idx = toks.index("-start")
    name_parts = []
    for tok in toks[idx + 1:]:
        if tok.startswith("-"):  # 遇到下一个 flag（-noWindow 等）停止
            break
        name_parts.append(tok)
    return " ".join(name_parts) if name_parts else None


def _listening_port(pid):
    """查进程 PID 监听的 TCP 端口（模拟器监听 5555+）。返回端口号字符串或 None。
    Mac/Linux 用 lsof（注意 -a 表示 AND，否则 -p 与 -iTCP 是 OR 会混入其它进程），
    Windows 用 netstat。"""
    is_win = platform.system() == "Windows"
    try:
        if is_win:
            rc, out = run_cmd('netstat -ano -p TCP', timeout=5)
            for line in out.splitlines():
                cols = line.split()
                # TCP  127.0.0.1:5555  0.0.0.0:0  LISTENING  <pid>
                if len(cols) >= 5 and "LISTEN" in cols[-2].upper() and cols[-1] == str(pid):
                    addr = cols[1]
                    if ":" in addr:
                        return addr.rsplit(":", 1)[-1]
        else:
            # 关键：-a 让 -p 和 -iTCP 取交集，否则会列出所有进程的 TCP
            rc, out = run_cmd(f"lsof -nP -a -p {pid} -iTCP -sTCP:LISTEN", timeout=5)
            for line in out.splitlines()[1:]:
                parts = line.split()
                # NAME 列形如 127.0.0.1:5555
                if len(parts) >= 9 and ":" in parts[8]:
                    return parts[8].rsplit(":", 1)[-1]
    except Exception:
        pass
    return None


def resolve_connect_key():
    """确定本服务要路由到的目标设备 connect-key。
    策略：
      优先用自动映射（EMULATOR_INSTANCE -> connect-key，靠 Emulator 进程监听端口），
      因为它比 hdc list targets 更稳定、更准确（后者有抖动，且多设备时不知哪个对哪个实例）。
      只要映射查到目标实例的 connect-key，就直接用它（不依赖 list_targets 的时序）。
      映射失败再退化为 list_targets。
      返回 (connect_key_or_None, reason)。"""
    global CURRENT_CONNECT_KEY

    # 优先级 1：自动映射 EMULATOR_INSTANCE -> connect-key（最可靠，单/多设备通用）
    # 关键：映射查到就用，不被 list_targets 的时序/抖动影响
    inst_map = build_instance_connectkey_map()
    mapped = inst_map.get(EMULATOR_INSTANCE)
    if mapped:
        CURRENT_CONNECT_KEY = mapped
        return mapped, "mapped"

    # 优先级 2：hdc list targets 实际看到的设备（映射没找到目标实例时）
    keys = list_targets()
    if not keys:
        CURRENT_CONNECT_KEY = None
        return None, "no_device"
    if len(keys) == 1:
        CURRENT_CONNECT_KEY = keys[0]
        return keys[0], "auto"

    # 优先级 3：config.py/环境变量指定的 connect-key（多设备且映射失败时）
    cfg_key = (CONNECT_KEY_CFG or "").strip()
    if cfg_key and cfg_key in keys:
        CURRENT_CONNECT_KEY = cfg_key
        return cfg_key, "configured"
    # 未明确指定：默认取第一个，但强烈提示用户多设备需指定
    CURRENT_CONNECT_KEY = keys[0]
    print(f"  [!] 检测到 {len(keys)} 台设备: {keys}")
    print(f"    映射未找到实例 '{EMULATOR_INSTANCE}'，当前默认使用第一台: {keys[0]}")
    print(f"    如需指定，设置环境变量 HDC_CONNECT_KEY（值为 hdc list targets 的 connect-key）后重启")
    return keys[0], "first_of_multi"

    # 多设备优先级 1：自动映射 EMULATOR_INSTANCE -> connect-key
    inst_map = build_instance_connectkey_map()
    mapped = inst_map.get(EMULATOR_INSTANCE)
    if mapped and mapped in keys:
        CURRENT_CONNECT_KEY = mapped
        print(f"  [OK] 多设备自动定位: 实例 '{EMULATOR_INSTANCE}' -> {mapped}")
        return mapped, "mapped"

    # 多设备优先级 2：config.py/环境变量指定的 connect-key
    cfg_key = (CONNECT_KEY_CFG or "").strip()
    if cfg_key and cfg_key in keys:
        CURRENT_CONNECT_KEY = cfg_key
        return cfg_key, "configured"
    # 未明确指定：默认取第一个，但强烈提示用户多设备需指定
    CURRENT_CONNECT_KEY = keys[0]
    print(f"  [!] 检测到 {len(keys)} 台设备: {keys}")
    print(f"    当前默认使用第一台: {keys[0]}")
    print(f"    如需指定其它设备，设置环境变量 HDC_CONNECT_KEY（值为 hdc list targets 的 connect-key）后重启")
    return keys[0], "first_of_multi"


def sleep_interruptible(seconds, stop_flag=None):
    """可被 Ctrl-C 即时中断的 sleep。
    用 0.05s 小睡循环：保证主线程的 SIGINT handler（已全局注册）在字节码间隙被调用，
    Mac/Windows 都能在 0.05s 内响应。可选传入 stop_flag dict 提前退出。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if stop_flag is not None and stop_flag.get("stop"):
            return
        try:
            time.sleep(0.05)
        except KeyboardInterrupt:
            raise  # 透传给上层处理


def wait_device_online(timeout=EMU_START_TIMEOUT, instance_name=None, stop_flag=None):
    """轮询直到「指定实例」对应的设备上线（hdc 能识别到它的 connect-key）。
    多设备场景下必须精确等 instance_name 那台，不能见任意设备就返回，
    否则若另一台已在跑，会误把它的 connect-key 当成本实例。
    返回 (connect_key_or_None, message)。Ctrl-C 或 stop_flag 可即时中断。"""
    deadline = time.time() + timeout
    attempt = 0
    last_running = None
    total_attempts = max(1, timeout // EMU_POLL_INTERVAL)
    while time.time() < deadline:
        # 用户中途 Ctrl-C：立即退出
        if stop_flag is not None and stop_flag.get("stop"):
            return None, "用户中断等待"
        attempt += 1
        # 1) 实例 isRunning 是否翻 true（emulator 进程已就绪）
        if instance_name:
            inst = find_instance_status(instance_name)
            if inst:
                last_running = inst["isRunning"]
        # 2) 精确等 instance_name 对应的 connect-key 出现
        #    关键：必须同时满足两个条件才算真正可用：
        #    a) 映射能查到 connect-key（Emulator 进程已监听端口）
        #    b) hdc list targets 也包含这个 key（hdc server 已登记设备）
        #    只满足 a 不满足 b 时，rport 会报 "Device not found or connected"（时序竞态）。
        if instance_name:
            inst_map = build_instance_connectkey_map()
            target_key = inst_map.get(instance_name)
            if target_key:
                # 还要确认 hdc server 已登记这台设备
                online_keys = list_targets()
                if target_key in online_keys:
                    return target_key, f"实例 '{instance_name}' 已上线: {target_key}"
                # 映射有但 hdc 没登记：继续等（不打点，下一轮再查）
        else:
            # 没指定实例名：退化为"任意设备上线"（兼容老用法）
            keys = list_targets()
            if keys:
                return keys[0], f"hdc 识别到设备: {keys[0]}"
        # 终端进度：每轮打一个点
        running_hint = "" if last_running is None else (f" [实例进程 {'就绪' if last_running else '启动中'}]")
        print(f"  . 等待模拟器上线{running_hint}（{attempt}/{total_attempts}）")
        # 可中断 sleep（0.05s 粒度，Ctrl-C 在此窗口内触发）
        sleep_interruptible(EMU_POLL_INTERVAL, stop_flag)
    return None, f"等待 {timeout}s 后设备仍未上线"


def ensure_emulator_running(stop_flag=None):
    """启动前置检查：确认目标实例运行中，没在跑就自动拉起并等设备上线。
    失败只打印警告、不抛异常，让 HTTP 服务保持可用（降级风格与现有 setup_fport 一致）。
    stop_flag: 共享 flag dict，Ctrl-C 时由全局 handler 置 {stop:True}，本函数提前返回。"""
    # Emulator 不可用直接跳过（无法探测/启动）
    if not os.path.isfile(EMULATOR):
        print(f"  [!] 找不到 Emulator，跳过自动启动检查（手动确认模拟器状态）: {EMULATOR}")
        return

    inst = find_instance_status(EMULATOR_INSTANCE)
    if inst is None:
        # 实例名不存在：列出可用实例名辅助排查
        print(f"  [!] 实例 '{EMULATOR_INSTANCE}' 不存在，无法自动启动")
        names = list_instance_names()
        if names:
            print(f"    可用实例: {', '.join(names)}")
        print(f"    用法: python3 emulator-control-server.py \"<实例名>\"")
        return

    if inst["isRunning"]:
        print(f"  [OK] 实例 '{EMULATOR_INSTANCE}' 已在运行")
        return

    # 未运行 -> 自动启动（无窗口）
    mode = "无窗口" if HEADLESS else "带窗口"
    print(f"  实例 '{EMULATOR_INSTANCE}' 未运行，自动启动中（{mode}模式）...")
    ok, msg = start_emulator(EMULATOR_INSTANCE)
    if not ok:
        print(f"  [X] 启动模拟器失败: {msg}")
        return
    print(f"  {msg}")
    print(f"  等待模拟器上线 / hdc 识别设备（最多 {EMU_START_TIMEOUT}s，按 Ctrl+C 可中断）...")
    try:
        key, msg = wait_device_online(EMU_START_TIMEOUT, EMULATOR_INSTANCE, stop_flag)
    except KeyboardInterrupt:
        # handler 已设置 stop_flag，标记一下让主循环直接进清理
        if stop_flag is not None:
            stop_flag["stop"] = True
        print(f"\n  [!] 用户中断等待，模拟器可能仍在后台启动中")
        return
    if key:
        print(f"  [OK] 模拟器已上线，{msg}")
    else:
        print(f"  [!] {msg}")
        print(f"    emulator-control-server 继续运行，但 hdc 转发可能失败")
        print(f"    模拟器就绪后重启本服务即可重建转发")


def setup_fport():
    """建立 hdc 反向端口转发（rport）：模拟器内访问 127.0.0.1:DEVICE_PORT -> 宿主机:PORT
    用不同端口避免与 emulator-control-server 监听冲突。
    多设备时自动用 -t <connect-key> 路由到目标实例。"""
    global CURRENT_CONNECT_KEY
    try:
        # 确认 hdc 可用
        rc, out = run_cmd([HDC, "version"], timeout=5)
        if rc != 0:
            print(f"  [X] hdc 不可用: {HDC}")
            print(f"    错误: {out}")
            return False

        # 确定目标设备 connect-key（0/1/多 设备三种情况）
        key, reason = resolve_connect_key()
        if not key:
            print(f"  [X] hdc 未识别到任何设备")
            print(f"    请确认模拟器已连接：{HDC} list targets")
            return False
        if reason == "auto":
            print(f"  [OK] 目标设备: {key}")
        elif reason == "mapped":
            print(f"  [OK] 目标设备（实例自动定位）: {key}")
        # reason == 'first_of_multi' 的警告已在 resolve_connect_key 里打印
        elif reason == "configured":
            print(f"  [OK] 目标设备（config.py/环境变量 指定）: {key}")

        # 清除可能存在的旧转发（fport rm 能同时清 fport 和 rport 建的转发）
        # 注意：rm 时端口组合是 "源 目标"
        for rm_args in [
            ["fport", "rm", f"tcp:{DEVICE_PORT}", f"tcp:{PORT}"],
            ["fport", "rm", f"tcp:{DEVICE_PORT}", f"tcp:{DEVICE_PORT}"],
            ["fport", "rm", f"tcp:{PORT}", f"tcp:{DEVICE_PORT}"],
        ]:
            hdc_cmd(rm_args, timeout=5)  # 忽略返回，清旧的而已

        # 建立 rport（设备内 DEVICE_PORT -> 宿主机 PORT）
        rc, output = hdc_cmd(["rport", f"tcp:{DEVICE_PORT}", f"tcp:{PORT}"], timeout=5)
        if rc == 0 and "OK" in output:
            return True
        else:
            print(f"  [X] hdc rport 建立失败: {output}")
            print(f"    请确认模拟器已连接：{HDC} list targets")
            return False
    except FileNotFoundError:
        print(f"  [X] 找不到 hdc: {HDC}")
        print(f"    请设置 HDC_PATH 环境变量指向 hdc.exe/hdc 的路径")
        return False
    except Exception as e:
        print(f"  [X] 建立端口转发异常: {e}")
        return False

# 允许的折叠状态
VALID_STATES = {"open", "half-open", "close"}

# 允许的旋转方向
VALID_ROTATIONS = {"left", "right"}


def do_fold(state):
    """执行 Emulator 折叠命令"""
    try:
        if platform.system() == "Windows":
            cmd = f'"{EMULATOR}" -instance "{EMULATOR_INSTANCE}" -foldedState {state}'
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, shell=True)
        else:
            result = subprocess.run(
                [EMULATOR, "-instance", EMULATOR_INSTANCE, "-foldedState", state],
                capture_output=True, text=True, timeout=10
            )
        output = (result.stdout or "") + (result.stderr or "")
        success = "success" in output
        if not success:
            # 打印完整输出便于排查
            print(f"    emulator 返回码: {result.returncode}")
            print(f"    emulator stdout: '{result.stdout}'")
            print(f"    emulator stderr: '{result.stderr}'")
        return success, output.strip()
    except Exception as e:
        return False, str(e)


def do_rotation(direction):
    """执行 Emulator 旋转命令"""
    try:
        if platform.system() == "Windows":
            cmd = f'"{EMULATOR}" -instance "{EMULATOR_INSTANCE}" -rotation {direction}'
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, shell=True)
        else:
            result = subprocess.run(
                [EMULATOR, "-instance", EMULATOR_INSTANCE, "-rotation", direction],
                capture_output=True, text=True, timeout=10
            )
        output = (result.stdout or "") + (result.stderr or "")
        success = "success" in output
        if not success:
            print(f"    emulator 返回码: {result.returncode}")
            print(f"    emulator stdout: '{result.stdout}'")
            print(f"    emulator stderr: '{result.stderr}'")
        return success, output.strip()
    except Exception as e:
        return False, str(e)


class FoldHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(self.path).query)

        # 折叠控制
        if self.path.startswith("/fold?"):
            state = query.get("state", [None])[0]
            if state not in VALID_STATES:
                self._respond(400, {"success": False, "error": f"无效状态: {state}"})
                return
            print(f"[{self.log_date_time_string()}] 触发折叠 -> {state}")
            success, msg = do_fold(state)
            if success:
                print(f"  [OK] 已切换到 {state}")
            else:
                print(f"  [X] 切换失败: {msg}")
            self._respond(200, {"success": success, "state": state, "message": msg})

        # 旋转控制
        elif self.path.startswith("/rotation?"):
            direction = query.get("direction", [None])[0]
            if direction not in VALID_ROTATIONS:
                self._respond(400, {"success": False, "error": f"无效方向: {direction}"})
                return
            print(f"[{self.log_date_time_string()}] 触发旋转 -> {direction}")
            success, msg = do_rotation(direction)
            if success:
                print(f"  [OK] 已旋转 {direction}")
            else:
                print(f"  [X] 旋转失败: {msg}")
            self._respond(200, {"success": success, "direction": direction, "message": msg})

        elif self.path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "unknown endpoint"})

    def _respond(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # 静默默认日志，用自定义 print


def get_local_ips():
    """获取本机所有 IPv4 地址，方便模拟器连接"""
    ips = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips


class Tee:
    """同时向多个流写入（日志文件 + 终端），让前台手动运行也能看到输出。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


def main():
    global EMULATOR_INSTANCE
    if len(sys.argv) > 1:
        EMULATOR_INSTANCE = sys.argv[1]

    # ===== 日志落盘 + 终端输出（之前所有 print 写入文件，现在同时写终端）=====
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'emulator-control-server.log')
    log_fp = open(log_path, 'w', buffering=1, encoding='utf-8')  # 行缓冲，实时落盘，UTF-8 避免 Windows 乱码
    sys.stdout = Tee(log_fp, sys.stdout)   # 既写日志文件，也写终端
    sys.stderr = Tee(log_fp, sys.stderr)

    print("=" * 50)
    print(f"折叠控制 HTTP 服务启动")
    print(f"  平台: {platform.system()}")
    print(f"  Emulator: {EMULATOR}")
    print(f"  hdc: {HDC}")
    print(f"  模拟器实例: {EMULATOR_INSTANCE}")
    print(f"  监听端口: {PORT}")
    print(f"  窗口模式: {'无窗口' if HEADLESS else '带 GUI 窗口'}")
    print(f"  日志文件: {log_path}")
    print(f"  配置文件: {os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.py')}")
    print_paths()
    print("")

    # ===== 先启动 HTTP 服务（让健康检查尽早通过，不被后续步骤阻塞）=====
    server = http.server.HTTPServer(("0.0.0.0", PORT), FoldHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"  HTTP 服务已就绪，监听端口 {PORT}")
    print("")

    # ===== 尽早注册 Ctrl-C 处理（在任何可能阻塞的步骤之前）=====
    # 这样无论用户在「自动启动模拟器等待」「建立转发」「主循环」哪个阶段按 Ctrl-C，
    # 都能立即设置 stop_flag，主线程在下个字节码间隙退出并进入清理。
    stop_flag = {"stop": False, "server": None, "server_thread": None}
    stop_flag["server"] = server
    stop_flag["server_thread"] = server_thread

    def _request_stop(signum=None, frame=None):
        stop_flag["stop"] = True

    signal.signal(signal.SIGINT, _request_stop)
    if platform.system() != "Windows":
        signal.signal(signal.SIGTERM, _request_stop)

    # ===== 确保目标模拟器实例在运行（没跑就自动拉起，等设备上线）=====
    # 此处若用户按 Ctrl-C，全局 handler 置 stop_flag，wait_device_online 内的
    # sleep_interruptible 在 0.05s 内返回，本函数随即返回，下面检查 stop_flag 跳过后续步骤。
    print("  检查模拟器实例状态...")
    if not stop_flag["stop"]:
        ensure_emulator_running(stop_flag)
    print("")

    # 若等待期间已按 Ctrl-C，跳过转发建立，直接进清理
    if not stop_flag["stop"]:
        # ===== 再建立 hdc 端口转发（多设备自动用 -t 路由）=====
        print("  建立 hdc 端口转发...")
        if setup_fport():
            print(f"  [OK] hdc 反向端口转发已建立（rport: 模拟器内 127.0.0.1:{DEVICE_PORT} -> 宿主机:{PORT}）")
        else:
            print(f"  [!] hdc 端口转发失败，设备端可能无法连接 emulator-control-server")
            print(f"    请确认模拟器已连接（hdc list target）并重试")
        print("")

    print(f"  连接方式: 模拟器内访问 127.0.0.1:{DEVICE_PORT}（通过 rport 转发）")
    print(f"  API: GET /fold?state=open|half-open|close")
    print(f"  API: GET /rotation?direction=left|right")
    print(f"  按 Ctrl+C 停止（会自动清理端口转发 + 残留进程）")
    print("=" * 50)
    sys.stdout.flush()

    # ===== 主循环：极短 sleep 轮询 stop_flag =====
    # handler 在 HTTP 启动后就已注册（见上方），无论用户在哪个阶段按 Ctrl-C，
    # stop_flag 都会被置 True，主循环每 0.05s 检查一次立即退出。
    # 不用 server_thread.join()（Windows 下不响应 Ctrl-C 会卡死），
    # 不用 Event.wait()（macOS 后台进程下不被 SIGINT 打断）。
    try:
        while not stop_flag["stop"]:
            time.sleep(0.05)
    finally:
        # 无论如何都执行清理：关闭 HTTP、移除本服务建的转发、调 clean.py 收尾
        print("\n服务已停止，开始清理...")
        do_shutdown(server, server_thread)


def do_shutdown(server, server_thread):
    """退出清理：关闭 HTTP 服务 + 清 hdc 转发 + 调 clean.py 子进程做彻底清理。
    放在 finally 里，保证 Ctrl-C / 异常 / kill 都能跑到。"""
    # 1) 先停 HTTP，释放 8766 端口（避免 clean.py 误杀自己）
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        pass
    if server_thread.is_alive():
        server_thread.join(timeout=2)

    # 2) 移除本服务建立的 hdc 反向转发（多设备时也用 -t 路由）
    try:
        hdc_cmd(["fport", "rm", f"tcp:{DEVICE_PORT}", f"tcp:{PORT}"], timeout=5)
        print("  [OK] 已移除 hdc 反向端口转发")
    except Exception:
        pass

    # 3) 调 clean.py 子进程做彻底清理（残留进程 / 残留转发 / 端口占用）
    #    用独立子进程：它能杀掉 8766 上的残留 emulator-control-server 而不影响本进程，
    #    因为我们在第 1 步已释放端口、即将退出。
    try:
        clean_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clean.py")
        if os.path.isfile(clean_script):
            print("  调用 clean.py 做端口/转发彻底清理...")
            subprocess.run(
                [sys.executable, clean_script],
                timeout=30,
                capture_output=False,
            )
        else:
            print(f"  [!] clean.py 不存在（{clean_script}），跳过彻底清理")
    except subprocess.TimeoutExpired:
        print("  [!] clean.py 执行超时（30s），强制继续退出")
    except Exception as e:
        print(f"  [!] 调用 clean.py 失败: {e}")

    print("已清理，再见")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
