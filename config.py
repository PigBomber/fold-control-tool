# ============================================================
# emulator-control-server 配置（直接改下面变量的值，重启 emulator-control-server.py / clean.py 生效）
# ============================================================
# 优先级（高 -> 低）：命令行参数 > 环境变量 > 本文件(config.py) > 代码内默认值
# 即本文件是默认配置，命令行/环境变量可临时覆盖，不改这里。
# 本文件入库，团队共享；个人要不同值用环境变量或命令行参数。
# ============================================================

# ---- 模拟器 ----
# 模拟器实例名（Emulator -list 列出的名字，如 Mate X7 / Pura X / MateBook Pro）
EMULATOR_INSTANCE = "Mate X7"

# 是否无窗口启动模拟器
#   False = 带 GUI 窗口（默认，可观察折叠动画/方向）
#   True  = 无窗口（省资源/CI）
HEADLESS = False

# 等待模拟器上线超时秒数（冷启动常见 30~90s，机器慢可调大）
EMU_START_TIMEOUT = 120


# ---- 网络 ----
# emulator-control-server 监听端口（宿主机）
PORT = 8766

# 设备内访问端口（模拟器内 FoldTrigger 访问的端口，通过 rport 转发到 PORT）
# 用不同端口避免与 emulator-control-server 监听冲突
DEVICE_PORT = 8765

# 多设备时显式指定目标设备的 connect-key（hdc list targets 的输出，如 127.0.0.1:5555）
# None = 自动选择（单设备直用，多设备取第一个并警告）
HDC_CONNECT_KEY = None


# ---- 路径 ----
# 以下留空字符串 "" = 自动探测。仅在自动探测失败时才需要手动指定。
EMULATOR_PATH = ""
HDC_PATH = ""
