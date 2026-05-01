import logging
import threading
import time
import psutil
import platform
import datetime
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

# ── 硬體抽象層：自動偵測 NVIDIA/AMD/Intel GPU 並調用對應 DLL ──
from hardware_provider import get_manager

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'npuview_secret_2024'
# threading 模式：與 ctypes (ADL/WMI) 呼叫完全相容
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*')

# 在 Flask 程序啟動時初始化硬體管理器（非 request context）
_hw = None

def hw():
    """取得硬體管理器（延遲初始化，thread-safe for single background thread）"""
    global _hw
    if _hw is None:
        _hw = get_manager()
        logger.info(f"[HW] {_hw.summary()}")
        logger.info(f"[HW] CPU 溫度來源: {_hw._cpu_temp._source}")
    return _hw

def get_cpu_info():
    cpu_percent_per_core = psutil.cpu_percent(percpu=True, interval=None)
    cpu_freq = psutil.cpu_freq()
    cpu_times = psutil.cpu_times_percent(interval=None)

    # CPU 溫度（經由 hardware_provider 多層 fallback）
    cpu_temp = hw().get_cpu_temp()

    return {
        'percent': psutil.cpu_percent(interval=None),
        'per_core': cpu_percent_per_core,
        'core_count_logical': psutil.cpu_count(logical=True),
        'core_count_physical': psutil.cpu_count(logical=False),
        'freq_current': round(cpu_freq.current, 1) if cpu_freq else 0,
        'freq_min': round(cpu_freq.min, 1) if cpu_freq else 0,
        'freq_max': round(cpu_freq.max, 1) if cpu_freq else 0,
        'user': round(cpu_times.user, 1),
        'system': round(cpu_times.system, 1),
        'idle': round(cpu_times.idle, 1),
        'temp_source': cpu_temp['source'],
        'temp_package': cpu_temp['package_temp'],
        'temp_cores': cpu_temp['core_temps'],
    }

def get_memory_info():
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    return {
        'total': round(vm.total / (1024 ** 3), 2),
        'available': round(vm.available / (1024 ** 3), 2),
        'used': round(vm.used / (1024 ** 3), 2),
        'percent': vm.percent,
        'swap_total': round(sm.total / (1024 ** 3), 2),
        'swap_used': round(sm.used / (1024 ** 3), 2),
        'swap_percent': sm.percent,
    }

def get_gpu_info():
    """透過 hardware_provider 取得 GPU 資料（自動選用最佳 API）"""
    try:
        return hw().get_gpu_list()
    except Exception as e:
        logger.warning(f"[GPU] 取得資料失敗: {e}")
        return []

def get_disk_info():
    partitions = psutil.disk_partitions()
    disks = []
    for p in partitions:
        try:
            usage = psutil.disk_usage(p.mountpoint)
            disks.append({
                'device': p.device,
                'mountpoint': p.mountpoint,
                'fstype': p.fstype,
                'total': round(usage.total / (1024 ** 3), 2),
                'used': round(usage.used / (1024 ** 3), 2),
                'free': round(usage.free / (1024 ** 3), 2),
                'percent': usage.percent,
            })
        except PermissionError:
            continue
    return disks

def get_network_info():
    net_io = psutil.net_io_counters()
    return {
        'bytes_sent': net_io.bytes_sent,
        'bytes_recv': net_io.bytes_recv,
        'packets_sent': net_io.packets_sent,
        'packets_recv': net_io.packets_recv,
    }

def get_system_info():
    uname = platform.uname()
    boot_time = datetime.datetime.fromtimestamp(psutil.boot_time())
    uptime = datetime.datetime.now() - boot_time
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    return {
        'os': f'{uname.system} {uname.release}',
        'hostname': uname.node,
        'processor': uname.processor or platform.processor(),
        'architecture': platform.architecture()[0],
        'python_version': platform.python_version(),
        'uptime': f'{hours:02d}:{minutes:02d}:{seconds:02d}',
        'boot_time': boot_time.strftime('%Y-%m-%d %H:%M:%S'),
    }

@app.route('/')
def index():
    system_info = get_system_info()
    mgr = hw()
    gpu_list = mgr.get_gpu_list()
    return render_template(
        'index.html',
        system_info=system_info,
        gpu_available=mgr.gpu_available,
        gpu_vendors=mgr.gpu_vendors,
        gpu_list=gpu_list,
        cpu_temp_source=mgr._cpu_temp._source,
    )

@socketio.on('connect')
def handle_connect():
    emit('connected', {'status': 'ok'})

@socketio.on('request_stats')
def handle_request_stats():
    data = _collect_stats()
    emit('stats_update', data)

def _collect_stats() -> dict:
    """在 OS 執行緒中收集所有硬體資料（ctypes/WMI 呼叫安全執行）"""
    return {
        'cpu':     get_cpu_info(),
        'memory':  get_memory_info(),
        'gpu':     get_gpu_info(),
        'disk':    get_disk_info(),
        'network': get_network_info(),
        'timestamp': datetime.datetime.now().strftime('%H:%M:%S'),
    }

def background_task():
    """在獨立 daemon 執行緒中每 2 秒收集硬體資料並推送"""
    while True:
        time.sleep(2)
        try:
            data = _collect_stats()
            socketio.emit('stats_update', data)
        except Exception as e:
            logger.warning(f"[BG] 資料收集失敗: {e}")

if __name__ == '__main__':
    hw()
    t = threading.Thread(target=background_task, daemon=True)
    t.start()
    print("=" * 50)
    print("  NPUView - System Monitor")
    print(f"  GPU: {hw().summary()}")
    print(f"  CPU 溫度來源: {hw()._cpu_temp._source}")
    print("  Open browser: http://localhost:2700")
    print("=" * 50)
    socketio.run(app, host='0.0.0.0', port=2700, debug=False,
                 use_reloader=False, allow_unsafe_werkzeug=True)
