#!/usr/bin/env python3
#
# 折叠屏测试 - 宿主机折叠控制 HTTP 服务（支持 Mac/Windows）
#
# 功能：监听 HTTP 请求，收到折叠指令后执行 Emulator 命令切换折叠状态。
#       测试代码内的 triggerFold() 通过 HTTP 直接调用本服务，同步等待结果。
#
# 用法：
#   python3 scripts/fold-server.py
#   python3 scripts/fold-server.py "Pura X"   # 指定实例名
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

# ============ 配置 ============
# fold-server 监听端口（宿主机）
PORT = 8766
# 设备内访问端口（模拟器内 FoldTrigger 访问的端口，通过 rport 转发到 PORT）
# 用不同端口避免与 fold-server 监听冲突
DEVICE_PORT = 8765

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

    # 候选路径
    candidates = []

    # 1. 从 DevEco 根目录找
    deveco_root = find_deveco_root()
    if deveco_root:
        candidates.extend([
            os.path.join(deveco_root, "tools", "emulator", exe_name),
            os.path.join(deveco_root, "Contents", "tools", "emulator", exe_name),
            os.path.join(deveco_root, "sdk", "tools", "emulator", exe_name),
        ])

    # 2. 环境变量 EMULATOR_PATH 直接指定
    env_path = os.environ.get("EMULATOR_PATH", "")
    if env_path:
        candidates.append(env_path)

    # 3. PATH 里找
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    for d in path_dirs:
        candidates.append(os.path.join(d, exe_name))

    # 验证
    for c in candidates:
        if c and os.path.isfile(c):
            return c

    print(f"  ⚠ 找不到 emulator，请设置 EMULATOR_PATH 环境变量")
    return exe_name  # 兜底，让命令失败时报错


def find_hdc():
    """自动探测 hdc 路径（不写死）"""
    exe_name = "hdc.exe" if platform.system() == "Windows" else "hdc"

    candidates = []

    # 1. 从 DevEco 根目录找（sdk/default/openharmony/toolchains/hdc）
    deveco_root = find_deveco_root()
    if deveco_root:
        candidates.extend([
            os.path.join(deveco_root, "sdk", "default", "openharmony", "toolchains", exe_name),
            os.path.join(deveco_root, "Contents", "sdk", "default", "openharmony", "toolchains", exe_name),
            os.path.join(deveco_root, "sdk", "openharmony", "toolchains", exe_name),
        ])

    # 2. 环境变量 HDC_PATH 直接指定
    env_path = os.environ.get("HDC_PATH", "")
    if env_path:
        candidates.append(env_path)
        candidates.append(os.path.join(env_path, exe_name))

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
EMULATOR_INSTANCE = os.environ.get("EMULATOR_INSTANCE", "Mate X7")

# 启动时打印探测到的路径（方便排查）
def print_paths():
    print(f"  路径探测:")
    print(f"    DevEco 根目录: {find_deveco_root() or '未找到（用环境变量 DEVECO_SDK_HOME 指定）'}")
    print(f"    Emulator: {EMULATOR}{'  ✓' if os.path.isfile(EMULATOR) else '  ✗ 未找到'}")
    print(f"    hdc: {HDC}{'  ✓' if os.path.isfile(HDC) else '  （用 PATH 兜底）'}")


def setup_fport():
    """建立 hdc 反向端口转发（rport）：模拟器内访问 127.0.0.1:DEVICE_PORT → 宿主机:PORT
    用不同端口避免与 fold-server 监听冲突。
    所有平台通用，模拟器内统一用 127.0.0.1 访问本服务。"""
    try:
        # 确认 hdc 可用
        check = subprocess.run([HDC, "version"] if platform.system() != "Windows" else f'"{HDC}" version',
                               capture_output=True, text=True, timeout=5,
                               shell=(platform.system() == "Windows"))
        if check.returncode != 0:
            print(f"  ✗ hdc 不可用: {HDC}")
            print(f"    错误: {check.stderr}")
            return False

        # 清除可能存在的旧转发（fport rm 能同时清 fport 和 rport 建的转发）
        # 注意：参数间用空格，不加引号；rm 时端口组合是 "源 目标"
        for cmd_rm in [
            f'fport rm tcp:{DEVICE_PORT} tcp:{PORT}',
            f'fport rm tcp:{DEVICE_PORT} tcp:{DEVICE_PORT}',
            f'fport rm tcp:{PORT} tcp:{DEVICE_PORT}',
        ]:
            if platform.system() == "Windows":
                subprocess.run(f'"{HDC}" {cmd_rm}', capture_output=True, text=True, timeout=5, shell=True)
            else:
                subprocess.run([HDC] + cmd_rm.split(), capture_output=True, text=True, timeout=5)

        # 建立 rport（设备内 DEVICE_PORT → 宿主机 PORT）
        cmd_rport = f'rport tcp:{DEVICE_PORT} tcp:{PORT}'
        if platform.system() == "Windows":
            result = subprocess.run(f'"{HDC}" {cmd_rport}', capture_output=True, text=True, timeout=5, shell=True)
        else:
            result = subprocess.run([HDC] + cmd_rport.split(), capture_output=True, text=True, timeout=5)

        output = (result.stdout or "") + (result.stderr or "")
        if "OK" in output:
            print(f"  ✓ hdc 反向端口转发已建立（rport: 模拟器内 127.0.0.1:{DEVICE_PORT} → 宿主机:{PORT}）")
            return True
        else:
            print(f"  ✗ hdc rport 建立失败: {output}")
            print(f"    请确认模拟器已连接：{HDC} list target")
            return False
    except FileNotFoundError:
        print(f"  ✗ 找不到 hdc: {HDC}")
        print(f"    请设置 HDC_PATH 环境变量指向 hdc.exe/hdc 的路径")
        return False
    except Exception as e:
        print(f"  ✗ 建立端口转发异常: {e}")
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
            print(f"[{self.log_date_time_string()}] 触发折叠 → {state}")
            success, msg = do_fold(state)
            if success:
                print(f"  ✓ 已切换到 {state}")
            else:
                print(f"  ✗ 切换失败: {msg}")
            self._respond(200, {"success": success, "state": state, "message": msg})

        # 旋转控制
        elif self.path.startswith("/rotation?"):
            direction = query.get("direction", [None])[0]
            if direction not in VALID_ROTATIONS:
                self._respond(400, {"success": False, "error": f"无效方向: {direction}"})
                return
            print(f"[{self.log_date_time_string()}] 触发旋转 → {direction}")
            success, msg = do_rotation(direction)
            if success:
                print(f"  ✓ 已旋转 {direction}")
            else:
                print(f"  ✗ 旋转失败: {msg}")
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


def main():
    global EMULATOR_INSTANCE
    if len(sys.argv) > 1:
        EMULATOR_INSTANCE = sys.argv[1]

    print("=" * 50)
    print(f"折叠控制 HTTP 服务启动")
    print(f"  平台: {platform.system()}")
    print(f"  Emulator: {EMULATOR}")
    print(f"  hdc: {HDC}")
    print(f"  模拟器实例: {EMULATOR_INSTANCE}")
    print(f"  监听端口: {PORT}")
    print(f"")

    # 建立 hdc 端口转发（所有平台通用，模拟器内 127.0.0.1 = 宿主机）
    print("  建立 hdc 端口转发...")
    setup_fport()

    print(f"")
    print(f"  连接方式: 模拟器内访问 127.0.0.1:{PORT}（通过 fport 转发到本服务）")
    print(f"  API: GET /fold?state=open|half-open|close")
    print(f"  API: GET /rotation?direction=left|right")
    print(f"  按 Ctrl+C 停止")
    print("=" * 50)

    server = http.server.HTTPServer(("0.0.0.0", PORT), FoldHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        # 清理端口转发
        try:
            cmd_rm = f'fport rm tcp:{DEVICE_PORT} tcp:{PORT}'
            if platform.system() == "Windows":
                subprocess.run(f'"{HDC}" {cmd_rm}', capture_output=True, text=True, timeout=5, shell=True)
            else:
                subprocess.run([HDC] + cmd_rm.split(), capture_output=True, text=True, timeout=5)
        except Exception:
            pass
        server.server_close()


if __name__ == "__main__":
    main()
