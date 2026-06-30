#!/usr/bin/env python3
#
# 折叠屏测试 - 端口与进程清理脚本（支持 Mac/Windows）
#
# 适用场景：fold-server 异常退出 / 启动失败导致 8766 端口被占用、
#           或 hdc rport 转发残留，新一次启动 bind 失败或转发冲突。
#
# 清理内容：
#   1. 杀掉占用 8766 端口的残留进程（fold-server.py 或其它）
#   2. 清除 hdc 的 8765↔8766 fport/rport 残留转发
#   3. 验证端口已释放、健康检查无响应
#
# 用法：
#   python3 clean.py
#
# 平台：Mac/Linux 用 lsof 定位端口；Windows 用 netstat。
#       kill 用 taskkill（Windows）/ kill（POSIX）。

import os
import sys
import socket
import platform
import subprocess

# ============ 配置（与 fold-server.py 保持一致）============
PORT = 8766
DEVICE_PORT = 8765


def is_windows():
    return platform.system() == "Windows"


def run(cmd, shell=False, timeout=5):
    """统一 subprocess.run 封装，返回 (returncode, stdout+stderr)。失败不抛异常。"""
    try:
        if is_windows() or shell:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=True)
        else:
            r = subprocess.run(cmd.split() if isinstance(cmd, str) else cmd,
                               capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return -1, f"命令不存在: {cmd}"
    except subprocess.TimeoutExpired:
        return -1, f"命令超时: {cmd}"
    except Exception as e:
        return -1, str(e)


# ============ 1. 端口占用清理 ============

def find_port_holders(port):
    """返回占用指定端口的 [(pid, process_name)] 列表。"""
    holders = []
    if is_windows():
        # netstat -ano: 找 LISTENING 的 PID
        rc, out = run(f'netstat -ano', timeout=5)
        if rc != 0:
            print(f"  ✗ netstat 查询失败: {out}")
            return holders
        for line in out.splitlines():
            cols = line.split()
            # 形如: TCP    0.0.0.0:8766     0.0.0.0:0     LISTENING     12345
            if len(cols) >= 5 and f":{port}" in cols[1] and "LISTEN" in cols[3].upper():
                pid = cols[-1]
                holders.append((pid, get_process_name(pid)))
    else:
        # Mac/Linux: lsof
        rc, out = run(f"lsof -nP -iTCP:{port}", timeout=5)
        if rc != 0:
            # lsof 无输出时返回码非 0，但可能只是端口没人占用（正常情况）
            if not out.strip():
                return holders
            print(f"  ⚠ lsof 查询异常: {out.strip()}")
            return holders
        for line in out.splitlines()[1:]:  # 跳过表头
            parts = line.split()
            if len(parts) >= 2:
                holders.append((parts[1], parts[0]))  # (PID, COMMAND)
    return holders


def get_process_name(pid):
    """Windows: 用 tasklist 反查进程名。"""
    if not is_windows():
        return ""
    rc, out = run(f'tasklist /FI "PID eq {pid}" /NH /FO CSV', timeout=5)
    if rc == 0 and out.strip():
        # 形如: "python.exe","12345",...
        return out.split(",")[0].strip('"')
    return ""


def kill_pid(pid):
    """跨平台杀进程，返回是否成功。"""
    if is_windows():
        rc, out = run(f'taskkill /F /PID {pid}', timeout=5)
    else:
        rc, out = run(f'kill -9 {pid}', timeout=5)
    return rc == 0, out.strip()


def clean_port(port):
    """清理占用 port 的进程。"""
    print(f"[1/3] 检查端口 {port} 占用...")
    holders = find_port_holders(port)
    if not holders:
        print(f"  ✓ 端口 {port} 无占用")
        return True

    ok = True
    for pid, name in holders:
        print(f"  发现占用: PID={pid} ({name})，正在清理...")
        success, msg = kill_pid(pid)
        if success:
            print(f"    ✓ 已终止 PID {pid}")
        else:
            print(f"    ✗ 终止失败 PID {pid}: {msg}")
            ok = False
    return ok


# ============ 2. hdc 端口转发清理 ============

def find_hdc():
    """探测 hdc 路径（优先用 fold-server.py 同款逻辑）。"""
    # 1. 直接 PATH 里找
    exe = "hdc.exe" if is_windows() else "hdc"
    rc, out = run(f"{exe} version", timeout=5)
    if rc == 0:
        return exe

    # 2. 从 DevEco 根目录找（Mac 常见路径）
    if not is_windows():
        candidates = [
            "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc",
            os.path.expanduser("~/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc"),
        ]
    else:
        candidates = []
        for env_var in ["DEVECO_SDK_HOME", "DEVECO_HOME"]:
            root = os.environ.get(env_var, "")
            if root:
                candidates.append(os.path.join(os.path.dirname(root), "sdk", "default", "openharmony", "toolchains", "hdc.exe"))
        for drive in ["C", "D", "E"]:
            candidates.append(f"{drive}:\\Program Files\\Huawei\\DevEco Studio\\sdk\\default\\openharmony\\toolchains\\hdc.exe")

    for c in candidates:
        if c and os.path.isfile(c):
            return c

    # 3. 环境变量 HDC_PATH
    env_path = os.environ.get("HDC_PATH", "")
    if env_path:
        if os.path.isfile(env_path):
            return env_path
        cand = os.path.join(env_path, exe)
        if os.path.isfile(cand):
            return cand

    return None


def clean_hdc_forwards(hdc):
    """清除所有涉及 8765/8766 的 hdc 转发。"""
    print(f"[2/3] 清除 hdc 端口转发（8765/8766）...")
    if hdc is None:
        print("  ⚠ 找不到 hdc（未配置 PATH / HDC_PATH），跳过转发清理")
        return False

    # 先列出当前转发
    rc, out = run(f'"{hdc}" fport ls' if is_windows() else f"{hdc} fport ls", timeout=5)
    if rc != 0:
        print(f"  ⚠ hdc fport ls 失败: {out.strip()}")
        return False

    if "Empty" in out or not out.strip():
        print("  ✓ 无 hdc 转发")
        return True

    # 找出涉及 8765/8766 的转发并逐条清除
    removed = 0
    for line in out.splitlines():
        # 形如: 127.0.0.1:5555    tcp:8765 tcp:8766    [Reverse]
        if "8765" not in line and "8766" not in line:
            continue
        # 提取 tcp:xx tcp:yy 两个端口串
        parts = line.split()
        tcp_pair = [p for p in parts if p.startswith("tcp:")]
        if len(tcp_pair) < 2:
            continue
        src, dst = tcp_pair[0], tcp_pair[1]
        rm_cmd = f'fport rm {src} {dst}'
        full = f'"{hdc}" {rm_cmd}' if is_windows() else f"{hdc} {rm_cmd}"
        rc2, out2 = run(full, timeout=5)
        if rc2 == 0:
            print(f"  ✓ 已清除转发: {src} → {dst}")
            removed += 1
        else:
            print(f"  ✗ 清除失败 {src} → {dst}: {out2.strip()}")

    if removed == 0:
        print("  ✓ 无涉及 8765/8766 的转发需清理")
    return True


# ============ 3. 健康检查验证 ============

def health_check(port):
    """检查 fold-server 是否还在响应。"""
    print(f"[3/3] 健康检查验证...")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", port))
        s.close()
        # 端口还能连上 = 还有进程在监听（清理未彻底）
        print(f"  ⚠ 端口 {port} 仍可连接，清理可能未彻底")
        return False
    except (ConnectionRefusedError, socket.timeout, OSError):
        print(f"  ✓ 端口 {port} 已释放，fold-server 未在响应")
        return True


def main():
    print("=" * 50)
    print("折叠屏测试 - 端口与进程清理")
    print(f"  平台: {platform.system()}")
    print(f"  目标端口: {PORT}（fold-server） / {DEVICE_PORT}（设备侧 rport）")
    print("=" * 50)

    # 1. 杀端口占用进程
    clean_port(PORT)

    # 2. 清 hdc 转发
    hdc = find_hdc()
    clean_hdc_forwards(hdc)

    # 3. 验证
    health_check(PORT)

    print("=" * 50)
    print("清理完成。现在可重新启动 fold-server:")
    print("  python3 fold-server.py")
    print("=" * 50)


if __name__ == "__main__":
    main()
