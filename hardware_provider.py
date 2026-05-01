"""
hardware_provider.py — NPUView 硬體抽象層
=========================================
自動偵測硬體廠商並調用對應 API / DLL：

  NVIDIA GPU  →  pynvml (wraps nvml.dll)
  AMD GPU     →  ctypes → atiadlxx.dll (ADL SDK)
  Intel GPU   →  WMI Win32_VideoController (基本資訊)
  CPU 溫度    →  LibreHardwareMonitor WMI  →  WMI MSAcpi
              →  OpenHardwareMonitor WMI  →  psutil fallback
"""

import ctypes
import ctypes.util
import logging
import os
import platform
import struct
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 資料結構
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GPUInfo:
    id: int = 0
    name: str = "Unknown GPU"
    vendor: str = "Unknown"          # NVIDIA / AMD / Intel
    api: str = "Unknown"             # pynvml / ADL / WMI / GPUtil
    load: float = 0.0                # %
    mem_total: float = 0.0           # MB
    mem_used: float = 0.0            # MB
    mem_free: float = 0.0            # MB
    mem_percent: float = 0.0         # %
    temperature: float = 0.0         # °C
    fan_speed: float = 0.0           # %  (-1 = 不支援)
    power_usage: float = -1.0        # W  (-1 = 不支援)
    driver: str = ""
    uuid: str = ""
    core_clock: int = 0              # MHz
    mem_clock: int = 0               # MHz

@dataclass
class CPUTempInfo:
    source: str = "N/A"
    package_temp: float = -1.0       # °C  (-1 = 取不到)
    core_temps: List[float] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# NVIDIA — pynvml
# ─────────────────────────────────────────────────────────────────────────────

class _NvidiaProvider:
    """使用 pynvml 直接呼叫 nvml.dll / libnvidia-ml.so"""

    def __init__(self):
        import pynvml
        self._nvml = pynvml
        pynvml.nvmlInit()
        self._count = pynvml.nvmlDeviceGetCount()
        logger.info(f"[NVIDIA/pynvml] 初始化成功，偵測到 {self._count} 個 GPU")

    def get_gpu_list(self) -> List[GPUInfo]:
        result = []
        nv = self._nvml
        for i in range(self._count):
            h = nv.nvmlDeviceGetHandleByIndex(i)

            # 基本資訊
            name = nv.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode()

            # 使用率
            util = nv.nvmlDeviceGetUtilizationRates(h)

            # 記憶體
            mem = nv.nvmlDeviceGetMemoryInfo(h)
            mem_total = mem.total / 1024 ** 2
            mem_used  = mem.used  / 1024 ** 2
            mem_free  = mem.free  / 1024 ** 2

            # 溫度
            try:
                temp = nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU)
            except Exception:
                temp = 0.0

            # 風扇
            try:
                fan = nv.nvmlDeviceGetFanSpeed(h)
            except Exception:
                fan = -1

            # 功耗
            try:
                power = nv.nvmlDeviceGetPowerUsage(h) / 1000.0
            except Exception:
                power = -1.0

            # 驅動
            try:
                driver = nv.nvmlSystemGetDriverVersion()
                if isinstance(driver, bytes):
                    driver = driver.decode()
            except Exception:
                driver = ""

            # UUID
            try:
                uuid = nv.nvmlDeviceGetUUID(h)
                if isinstance(uuid, bytes):
                    uuid = uuid.decode()
            except Exception:
                uuid = ""

            # 時脈
            try:
                core_clk = nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_GRAPHICS)
                mem_clk  = nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_MEM)
            except Exception:
                core_clk = mem_clk = 0

            result.append(GPUInfo(
                id=i, name=name, vendor="NVIDIA", api="pynvml",
                load=util.gpu,
                mem_total=round(mem_total, 1),
                mem_used=round(mem_used, 1),
                mem_free=round(mem_free, 1),
                mem_percent=round(mem_used / mem_total * 100, 1) if mem_total else 0,
                temperature=float(temp),
                fan_speed=float(fan),
                power_usage=round(power, 1),
                driver=driver, uuid=uuid,
                core_clock=core_clk, mem_clock=mem_clk,
            ))
        return result


# ─────────────────────────────────────────────────────────────────────────────
# AMD — ADL (atiadlxx.dll) via ctypes
# ─────────────────────────────────────────────────────────────────────────────

# ADL return codes
ADL_OK = 0

# ADL structs
class _ADLTemperature(ctypes.Structure):
    _fields_ = [("iSize", ctypes.c_int), ("iTemperature", ctypes.c_int)]

class _ADLPMActivity(ctypes.Structure):
    _fields_ = [
        ("iSize",                     ctypes.c_int),
        ("iEngineClock",              ctypes.c_int),   # 10 KHz
        ("iMemoryClock",              ctypes.c_int),   # 10 KHz
        ("iVddc",                     ctypes.c_int),   # mV
        ("iActivityPercent",          ctypes.c_int),   # %
        ("iCurrentPerformanceLevel",  ctypes.c_int),
        ("iCurrentBusSpeed",          ctypes.c_int),
        ("iCurrentBusLanes",          ctypes.c_int),
        ("iMaximumBusLanes",          ctypes.c_int),
        ("iReserved",                 ctypes.c_int),
    ]

ADL_MAX_PATH = 256

class _ADLAdapterInfo(ctypes.Structure):
    _fields_ = [
        ("iSize",          ctypes.c_int),
        ("iAdapterIndex",  ctypes.c_int),
        ("strUDID",        ctypes.c_char * ADL_MAX_PATH),
        ("iBusNumber",     ctypes.c_int),
        ("iDeviceNumber",  ctypes.c_int),
        ("iFunctionNumber",ctypes.c_int),
        ("iVendorID",      ctypes.c_int),
        ("strAdapterName", ctypes.c_char * ADL_MAX_PATH),
        ("strDisplayName", ctypes.c_char * ADL_MAX_PATH),
        ("iPresent",       ctypes.c_int),
        ("iExist",         ctypes.c_int),
        ("strDriverPath",  ctypes.c_char * ADL_MAX_PATH),
        ("strDriverPathExt", ctypes.c_char * ADL_MAX_PATH),
        ("strPNPString",   ctypes.c_char * ADL_MAX_PATH),
        ("iOSDisplayIndex",ctypes.c_int),
    ]

class _ADLMemoryInfo(ctypes.Structure):
    _fields_ = [
        ("iMemorySize",       ctypes.c_longlong),
        ("strMemoryType",     ctypes.c_char * ADL_MAX_PATH),
        ("iMemoryBandwidth",  ctypes.c_longlong),
    ]

# OverdriveN (Overdrive8) 結構 — RDNA / RDNA2 / Vega / Polaris
class _ADLODNPerformanceStatus(ctypes.Structure):
    _fields_ = [
        ("iCoreClock",                      ctypes.c_int),   # 10 KHz → /100 = MHz
        ("iMemoryClock",                    ctypes.c_int),   # 10 KHz → /100 = MHz
        ("iDCEFClock",                      ctypes.c_int),
        ("iGFXClock",                       ctypes.c_int),
        ("iUVDClock",                       ctypes.c_int),
        ("iVCEClock",                       ctypes.c_int),
        ("iGPUActivityPercent",             ctypes.c_int),   # %
        ("iCurrentCorePerformanceLevel",    ctypes.c_int),
        ("iCurrentMemoryPerformanceLevel",  ctypes.c_int),
        ("iCurrentDCEFPerformanceLevel",    ctypes.c_int),
        ("iCurrentGFXPerformanceLevel",     ctypes.c_int),
        ("iUVDPerformanceLevel",            ctypes.c_int),
        ("iVCEPerformanceLevel",            ctypes.c_int),
        ("iCurrentBusSpeed",                ctypes.c_int),
        ("iCurrentBusLanes",                ctypes.c_int),
        ("iMaximumBusLanes",                ctypes.c_int),
        ("iVDDC",                           ctypes.c_int),
        ("iVDDCI",                          ctypes.c_int),
    ]

class _ADLODNFanControl(ctypes.Structure):
    _fields_ = [
        ("iMode",              ctypes.c_int),
        ("iFanControlMode",    ctypes.c_int),
        ("iCurrentFanSpeedMode", ctypes.c_int),
        ("iCurrentFanSpeed",   ctypes.c_int),  # % or RPM
        ("iTargetFanSpeed",    ctypes.c_int),
        ("iTargetTemperature", ctypes.c_int),
        ("iMinPerformanceClock", ctypes.c_int),
        ("iMinFanLimit",       ctypes.c_int),
    ]

# PM Log 結構 — RDNA / RDNA2 (RX 5000 / RX 6000 series)
ADL_PMLOG_MAX_SENSORS = 256

class _ADLSingleSensorData(ctypes.Structure):
    _fields_ = [("supported", ctypes.c_int), ("value", ctypes.c_int)]

class _ADLPMLogDataOutput(ctypes.Structure):
    _fields_ = [
        ("ulNumValidSamples", ctypes.c_int),
        ("sensors", _ADLSingleSensorData * ADL_PMLOG_MAX_SENSORS),
    ]

# PM Log 感應器 ID (AMD ADL SDK)
ADL_PMLOG_CLK_GFXCLK           = 1    # GPU 核心時脈 (MHz)
ADL_PMLOG_CLK_MEMCLK            = 2    # 記憶體時脈 (MHz)
ADL_PMLOG_TEMPERATURE_EDGE      = 7    # 核心邊緣溫度 (°C)
ADL_PMLOG_TEMPERATURE_MEM       = 8    # GDDR 溫度 (°C)
ADL_PMLOG_FAN_RPM               = 12   # 風扇轉速 (RPM)
ADL_PMLOG_FAN_PERCENTAGE        = 13   # 風扇轉速 (%)
ADL_PMLOG_CURRENT_SOCKETPOWER   = 14   # Socket 功耗 (1/100 W → ÷100 = W)
ADL_PMLOG_INFO_ACTIVITY_GFX     = 15   # GPU 活動度 (%)  ← RDNA2 主要使用此項
ADL_PMLOG_INFO_ACTIVITY_MEM     = 16   # 記憶體活動度 (%)
ADL_PMLOG_INFO_VOLTAGE_GFX      = 21   # GPU 電壓 (mV)
ADL_PMLOG_GFX_BUSY              = 22   # GPU 忙碌度 (%) — 部分卡不支援
ADL_PMLOG_TEMPERATURE_HOTSPOT   = 27   # GPU Hotspot 溫度 (°C)
ADL_PMLOG_GPU_POWER             = 28   # 功耗 (1/100 W → ÷100 = W)

# malloc callback 必須保持引用，避免 GC
_ADL_MALLOC_CB = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_int)

class _AMDProvider:
    """
    使用 AMD ADL (Display Library) DLL 監控 AMD GPU
    DLL 路徑：atiadlxx.dll (64-bit) / atiadlxy.dll (32-bit)
    """

    # 固定記憶體分配緩衝區 pool（ADL callback 用）
    _buf_pool: list = []

    def __init__(self):
        self._adl = self._load_dll()
        self._ctx = ctypes.c_void_p(None)
        self._malloc_cb = _ADL_MALLOC_CB(self._malloc_callback)
        self._init_adl()
        self._adapters = self._enumerate_adapters()
        logger.info(f"[AMD/ADL] 初始化成功，偵測到 {len(self._adapters)} 個活躍顯示卡")

    @staticmethod
    def _load_dll() -> ctypes.CDLL:
        names = ["atiadlxx.dll", "atiadlxy.dll"]
        for name in names:
            try:
                dll = ctypes.cdll.LoadLibrary(name)
                logger.info(f"[AMD/ADL] 已載入 {name}")
                return dll
            except OSError:
                continue
        raise OSError("找不到 AMD ADL DLL (atiadlxx.dll / atiadlxy.dll)")

    @classmethod
    def _malloc_callback(cls, size: int) -> Optional[int]:
        buf = ctypes.create_string_buffer(size)
        cls._buf_pool.append(buf)          # 防止 GC
        return ctypes.addressof(buf)

    def _init_adl(self):
        fn = self._adl.ADL2_Main_Control_Create
        fn.restype  = ctypes.c_int
        fn.argtypes = [_ADL_MALLOC_CB, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        ret = fn(self._malloc_cb, 1, ctypes.byref(self._ctx))
        if ret != ADL_OK:
            raise RuntimeError(f"ADL2_Main_Control_Create 失敗，返回碼: {ret}")

    def _enumerate_adapters(self) -> list:
        """回傳 [(adapter_index, name, udid)] 僅活躍的顯示卡"""
        fn_count = self._adl.ADL2_Adapter_NumberOfAdapters_Get
        fn_count.restype  = ctypes.c_int
        fn_count.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]

        n = ctypes.c_int(0)
        if fn_count(self._ctx, ctypes.byref(n)) != ADL_OK or n.value <= 0:
            return []

        AdapterArray = _ADLAdapterInfo * n.value
        arr = AdapterArray()

        fn_info = self._adl.ADL2_Adapter_AdapterInfo_Get
        fn_info.restype  = ctypes.c_int
        fn_info.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ADLAdapterInfo),
            ctypes.c_int,
        ]
        fn_info(self._ctx, arr, ctypes.sizeof(arr))

        fn_active = self._adl.ADL2_Adapter_Active_Get
        fn_active.restype  = ctypes.c_int
        fn_active.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]

        # 以 UDID 去重複，只保留活躍的
        seen = set()
        adapters = []
        for info in arr:
            udid = info.strUDID.decode(errors="ignore")
            if udid in seen:
                continue
            seen.add(udid)
            status = ctypes.c_int(0)
            fn_active(self._ctx, info.iAdapterIndex, ctypes.byref(status))
            if status.value:
                adapters.append({
                    "index": info.iAdapterIndex,
                    "name":  info.strAdapterName.decode(errors="ignore").strip(),
                    "udid":  udid,
                })
        return adapters

    def _get_temperature(self, idx: int) -> float:
        fn = self._adl.ADL2_Overdrive5_Temperature_Get
        fn.restype  = ctypes.c_int
        fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                       ctypes.POINTER(_ADLTemperature)]
        t = _ADLTemperature()
        t.iSize = ctypes.sizeof(t)
        if fn(self._ctx, idx, 0, ctypes.byref(t)) == ADL_OK:
            return t.iTemperature / 1000.0
        return 0.0

    def _get_activity(self, idx: int) -> _ADLPMActivity:
        fn = self._adl.ADL2_Overdrive5_CurrentActivity_Get
        fn.restype  = ctypes.c_int
        fn.argtypes = [ctypes.c_void_p, ctypes.c_int,
                       ctypes.POINTER(_ADLPMActivity)]
        a = _ADLPMActivity()
        a.iSize = ctypes.sizeof(a)
        fn(self._ctx, idx, ctypes.byref(a))
        return a

    def _get_memory_info(self, idx: int) -> _ADLMemoryInfo:
        fn = self._adl.ADL2_Adapter_MemoryInfo_Get
        fn.restype  = ctypes.c_int
        fn.argtypes = [ctypes.c_void_p, ctypes.c_int,
                       ctypes.POINTER(_ADLMemoryInfo)]
        m = _ADLMemoryInfo()
        fn(self._ctx, idx, ctypes.byref(m))
        return m

    def _get_vram_usage_mb(self, idx: int) -> float:
        """ADL2_Adapter_DedicatedVRAMUsage_Get (只在較新 ADL 版本有)"""
        try:
            fn = self._adl.ADL2_Adapter_DedicatedVRAMUsage_Get
            fn.restype  = ctypes.c_int
            fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
            used = ctypes.c_int(0)
            if fn(self._ctx, idx, ctypes.byref(used)) == ADL_OK:
                return float(used.value)
        except AttributeError:
            pass
        return 0.0

    def _get_fan_speed(self, idx: int) -> float:
        try:
            from ctypes import c_int, byref, sizeof
            class _FanSpeedValue(ctypes.Structure):
                _fields_ = [("iSize", c_int), ("iSpeedType", c_int),
                             ("iFanSpeed", c_int), ("iFlags", c_int)]
            fn = self._adl.ADL2_Overdrive5_FanSpeed_Get
            fn.restype  = ctypes.c_int
            fn.argtypes = [ctypes.c_void_p, c_int, c_int,
                           ctypes.POINTER(_FanSpeedValue)]
            fsv = _FanSpeedValue()
            fsv.iSize = sizeof(fsv)
            fsv.iSpeedType = 1  # ADL_DL_FANCTRL_SPEED_TYPE_PERCENT
            if fn(self._ctx, idx, 0, byref(fsv)) == ADL_OK:
                return float(fsv.iFanSpeed)
        except Exception:
            pass
        return -1.0

    def _get_temperature_overdriven(self, idx: int) -> float:
        """ADL2_OverdriveN_Temperature_Get — RDNA/RDNA2/Vega"""
        try:
            fn = self._adl.ADL2_OverdriveN_Temperature_Get
            fn.restype  = ctypes.c_int
            fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                           ctypes.POINTER(ctypes.c_int)]
            t = ctypes.c_int(0)
            # type 1 = EDGE (GPU核心溫度)
            if fn(self._ctx, idx, 1, ctypes.byref(t)) == ADL_OK and t.value > 0:
                # 部分驅動版本回傳 millidegrees，部分直接回傳 degrees
                val = float(t.value)
                return val / 1000.0 if val > 1000 else val
        except Exception:
            pass
        return 0.0

    def _get_performance_overdriven(self, idx: int):
        """ADL2_OverdriveN_PerformanceStatus_Get — 取得時脈與 GPU 活動"""
        try:
            fn = self._adl.ADL2_OverdriveN_PerformanceStatus_Get
            fn.restype  = ctypes.c_int
            fn.argtypes = [ctypes.c_void_p, ctypes.c_int,
                           ctypes.POINTER(_ADLODNPerformanceStatus)]
            st = _ADLODNPerformanceStatus()
            if fn(self._ctx, idx, ctypes.byref(st)) == ADL_OK:
                return st
        except Exception:
            pass
        return None

    def _get_fan_overdriven(self, idx: int) -> float:
        """ADL2_OverdriveN_FanControl_Get — 取得風扇轉速 %"""
        try:
            fn = self._adl.ADL2_OverdriveN_FanControl_Get
            fn.restype  = ctypes.c_int
            fn.argtypes = [ctypes.c_void_p, ctypes.c_int,
                           ctypes.POINTER(_ADLODNFanControl)]
            fc = _ADLODNFanControl()
            if fn(self._ctx, idx, ctypes.byref(fc)) == ADL_OK:
                return float(fc.iCurrentFanSpeed)
        except Exception:
            pass
        return -1.0

    def _get_pmlog_data(self, idx: int):
        """ADL2_New_QueryPMLogData_Get — RDNA/RDNA2 全感應器資料"""
        try:
            fn = self._adl.ADL2_New_QueryPMLogData_Get
            fn.restype  = ctypes.c_int
            fn.argtypes = [ctypes.c_void_p, ctypes.c_int,
                           ctypes.POINTER(_ADLPMLogDataOutput)]
            out = _ADLPMLogDataOutput()
            if fn(self._ctx, idx, ctypes.byref(out)) == ADL_OK:
                return out
        except Exception:
            pass
        return None

    def get_gpu_list(self) -> List[GPUInfo]:
        result = []
        for i, adapter in enumerate(self._adapters):
            idx  = adapter["index"]
            mem  = self._get_memory_info(idx)
            vram_used = self._get_vram_usage_mb(idx)

            # 初始值
            load = 0.0; temp = 0.0; fan = -1.0; power = -1.0
            core_clk = 0; mem_clk = 0

            # 1. PM Log (RDNA/RDNA2 最優先)
            pmlog = self._get_pmlog_data(idx)
            if pmlog and pmlog.ulNumValidSamples > 0:
                s = pmlog.sensors
                def _sv(sid): return s[sid].value if s[sid].supported else None

                # 時脈
                v = _sv(ADL_PMLOG_CLK_GFXCLK);   core_clk = int(v) if v is not None else core_clk
                v = _sv(ADL_PMLOG_CLK_MEMCLK);    mem_clk  = int(v) if v is not None else mem_clk

                # 溫度：EDGE(7) → HOTSPOT(27) → (保持 0)
                v = _sv(ADL_PMLOG_TEMPERATURE_EDGE)
                if v is None: v = _sv(ADL_PMLOG_TEMPERATURE_HOTSPOT)
                if v is not None: temp = float(v)

                # 風扇
                v = _sv(ADL_PMLOG_FAN_PERCENTAGE)
                if v is None: v = _sv(ADL_PMLOG_FAN_RPM)
                if v is not None: fan = float(v)

                # 功耗 (1/100 W → W)
                v = _sv(ADL_PMLOG_GPU_POWER)
                if v is None: v = _sv(ADL_PMLOG_CURRENT_SOCKETPOWER)
                if v is not None: power = float(v) / 100.0

                # GPU 活動度：優先 INFO_ACTIVITY_GFX(15)，再 GFX_BUSY(22)，最後 OD5
                v = _sv(ADL_PMLOG_INFO_ACTIVITY_GFX)
                if v is None: v = _sv(ADL_PMLOG_GFX_BUSY)
                if v is not None:
                    load = float(v)
                else:
                    od5_act = self._get_activity(idx)
                    load = float(od5_act.iActivityPercent)

                logger.debug(f"[ADL PMLog] idx={idx} load={load} temp={temp} fan={fan} power={power} core={core_clk} mem={mem_clk}")
            else:
                # 2. OverdriveN (Vega/Polaris)
                odn_perf = self._get_performance_overdriven(idx)
                od5_act  = self._get_activity(idx)
                if odn_perf is not None and (odn_perf.iCoreClock > 0 or odn_perf.iGPUActivityPercent > 0):
                    load     = float(odn_perf.iGPUActivityPercent)
                    core_clk = odn_perf.iCoreClock  // 100
                    mem_clk  = odn_perf.iMemoryClock // 100
                else:
                    load     = float(od5_act.iActivityPercent)
                    core_clk = od5_act.iEngineClock  // 100
                    mem_clk  = od5_act.iMemoryClock  // 100
                # 溫度
                temp = self._get_temperature_overdriven(idx)
                if temp <= 0:
                    temp = self._get_temperature(idx)
                # 風扇
                fan = self._get_fan_overdriven(idx)
                if fan < 0:
                    fan = self._get_fan_speed(idx)

            mem_total   = mem.iMemorySize / 1024 ** 2 if mem.iMemorySize > 0 else 0
            mem_used_mb = vram_used if vram_used > 0 else 0
            mem_free_mb = max(mem_total - mem_used_mb, 0)

            result.append(GPUInfo(
                id=i,
                name=adapter["name"] or f"AMD GPU {i}",
                vendor="AMD", api="ADL",
                load=load,
                mem_total=round(mem_total, 1),
                mem_used=round(mem_used_mb, 1),
                mem_free=round(mem_free_mb, 1),
                mem_percent=round(mem_used_mb / mem_total * 100, 1) if mem_total else 0,
                temperature=temp,
                fan_speed=fan if fan >= 0 else -1.0,
                power_usage=power if power >= 0 else -1.0,
                driver="", uuid=adapter["udid"],
                core_clock=core_clk,
                mem_clock=mem_clk,
            ))
        return result

    def __del__(self):
        try:
            fn = self._adl.ADL2_Main_Control_Destroy
            fn.restype  = ctypes.c_int
            fn.argtypes = [ctypes.c_void_p]
            fn(self._ctx)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Intel / 其他 GPU — WMI Win32_VideoController (僅基本資訊)
# ─────────────────────────────────────────────────────────────────────────────

class _WMIGPUProvider:
    """Fallback: 透過 WMI 取得 GPU 基本資訊（不含即時使用率）"""

    def __init__(self):
        import wmi as _wmi
        self._wmi = _wmi.WMI()

    def get_gpu_list(self) -> List[GPUInfo]:
        # WMI COM 物件不是執行緒安全的，每個執行緒需各自 CoInitialize
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        try:
            import wmi as _wmi
            w = _wmi.WMI()
            result = []
            for i, vc in enumerate(w.Win32_VideoController()):
                name   = vc.Name or f"GPU {i}"
                vendor = self._detect_vendor(name)
                ram    = int(vc.AdapterRAM or 0) / 1024 ** 2
                result.append(GPUInfo(
                    id=i, name=name, vendor=vendor, api="WMI",
                    mem_total=round(ram, 1),
                    driver=vc.DriverVersion or "",
                ))
            return result
        except Exception as e:
            logger.debug(f"[WMI GPU] 取得資料失敗（非主執行緒）: {e}")
            return []

    @staticmethod
    def _detect_vendor(name: str) -> str:
        n = name.upper()
        if "NVIDIA" in n: return "NVIDIA"
        if "AMD" in n or "RADEON" in n or "ATI" in n: return "AMD"
        if "INTEL" in n: return "Intel"
        return "Unknown"


# ─────────────────────────────────────────────────────────────────────────────
# GPUtil fallback (NVIDIA only)
# ─────────────────────────────────────────────────────────────────────────────

class _GPUtilProvider:
    def __init__(self):
        import GPUtil
        self._GPUtil = GPUtil

    def get_gpu_list(self) -> List[GPUInfo]:
        result = []
        for g in self._GPUtil.getGPUs():
            result.append(GPUInfo(
                id=g.id, name=g.name, vendor="NVIDIA", api="GPUtil",
                load=round(g.load * 100, 1),
                mem_total=round(g.memoryTotal, 1),
                mem_used=round(g.memoryUsed, 1),
                mem_free=round(g.memoryFree, 1),
                mem_percent=round(g.memoryUtil * 100, 1),
                temperature=float(g.temperature),
                fan_speed=-1.0, power_usage=-1.0,
                driver=g.driver, uuid=g.uuid,
            ))
        return result


# ─────────────────────────────────────────────────────────────────────────────
# CPU 溫度 — 多層 fallback
# ─────────────────────────────────────────────────────────────────────────────

class _CPUTempProvider:
    """
    優先順序:
    1. LibreHardwareMonitor WMI namespace (root\\LibreHardwareMonitor)
    2. OpenHardwareMonitor  WMI namespace (root\\OpenHardwareMonitor)
    3. WMI MSAcpi_ThermalZoneTemperature
    4. psutil.sensors_temperatures() (Linux / macOS)
    """

    def __init__(self):
        self._source, self._get_fn = self._find_provider()
        logger.info(f"[CPU Temp] 使用來源: {self._source}")
        self._retry_at = 0   # epoch 秒，0 表示已有來源不需重試
        if self._source == "N/A":
            import time
            self._retry_at = time.time() + 30  # 30 秒後重試

    def _find_provider(self):
        # 1. LibreHardwareMonitor
        fn = self._try_lhm("root\\LibreHardwareMonitor")
        if fn: return "LibreHardwareMonitor", fn

        # 2. OpenHardwareMonitor
        fn = self._try_lhm("root\\OpenHardwareMonitor")
        if fn: return "OpenHardwareMonitor", fn

        # 3. MSAcpi
        fn = self._try_acpi()
        if fn: return "WMI-ACPI", fn

        # 4. psutil
        fn = self._try_psutil()
        if fn: return "psutil", fn

        # 5. HWiNFO64 共享記憶體
        fn = self._try_hwinfo64()
        if fn: return "HWiNFO64", fn

        return "N/A", lambda: CPUTempInfo(source="N/A")

    @staticmethod
    def _try_lhm(ns: str):
        try:
            import wmi as _wmi
            w = _wmi.WMI(namespace=ns)
            sensors = w.Sensor()
            cpu_temps = [s for s in sensors
                         if s.SensorType == "Temperature"
                         and "cpu" in s.Name.lower()]
            if not cpu_temps:
                return None

            def _get():
                try:
                    import pythoncom
                    pythoncom.CoInitialize()
                except Exception:
                    pass
                try:
                    w2 = _wmi.WMI(namespace=ns)
                    sensors2 = w2.Sensor()
                    temps = [float(s.Value) for s in sensors2
                             if s.SensorType == "Temperature"
                             and "cpu" in s.Name.lower()]
                    pkg = next((float(s.Value) for s in sensors2
                                if s.SensorType == "Temperature"
                                and "package" in s.Name.lower()), -1.0)
                    return CPUTempInfo(source=ns.split("\\")[-1],
                                      package_temp=pkg, core_temps=temps)
                except Exception:
                    return CPUTempInfo(source="N/A")
            return _get
        except Exception:
            return None

    @staticmethod
    def _try_acpi():
        try:
            import wmi as _wmi
            w = _wmi.WMI(namespace="root\\wmi")
            tzs = w.MSAcpi_ThermalZoneTemperature()
            if not tzs:
                return None

            def _get():
                try:
                    import pythoncom
                    pythoncom.CoInitialize()
                except Exception:
                    pass
                try:
                    w2 = _wmi.WMI(namespace="root\\wmi")
                    tzs2 = w2.MSAcpi_ThermalZoneTemperature()
                    temps = [(t.CurrentTemperature - 2732) / 10.0 for t in tzs2]
                    return CPUTempInfo(source="WMI-ACPI",
                                      package_temp=temps[0] if temps else -1.0,
                                      core_temps=temps)
                except Exception:
                    return CPUTempInfo(source="N/A")
            return _get
        except Exception:
            return None

    @staticmethod
    def _try_psutil():
        try:
            import psutil
            data = psutil.sensors_temperatures()
            if not data:
                return None

            def _get():
                try:
                    d = psutil.sensors_temperatures()
                    all_temps = []
                    pkg = -1.0
                    for name, entries in d.items():
                        for e in entries:
                            if "package" in (e.label or name).lower():
                                pkg = e.current
                            all_temps.append(e.current)
                    return CPUTempInfo(source="psutil",
                                      package_temp=pkg, core_temps=all_temps)
                except Exception:
                    return CPUTempInfo(source="N/A")
            return _get
        except Exception:
            return None

    @staticmethod
    def _try_hwinfo64():
        """讀取 HWiNFO64 共享記憶體 (HWiNFO_SENS_SM2) 取得 CPU 溫度。"""
        try:
            import ctypes, ctypes.wintypes, struct
            SM_NAMES = ("HWiNFO_SENS_SM2", "Global\\HWiNFO_SENS_SM2")
            HWINFO_SENSORS_STRING_LEN2 = 128
            HWINFO_UNIT_STRING_LEN     = 16

            # 嘗試開啟共享記憶體
            INVALID_HANDLE = ctypes.c_void_p(-1).value
            selected_name = None
            hMap = None
            for name in SM_NAMES:
                tmp = ctypes.windll.kernel32.OpenFileMappingW(
                    0x0004,   # FILE_MAP_READ
                    False,
                    name
                )
                if tmp and tmp != INVALID_HANDLE:
                    hMap = tmp
                    selected_name = name
                    break
            if not hMap:
                return None
            ctypes.windll.kernel32.CloseHandle(hMap)

            def _get():
                try:
                    import ctypes, struct
                    INVALID_HANDLE = ctypes.c_void_p(-1).value
                    hMap = ctypes.windll.kernel32.OpenFileMappingW(0x0004, False, selected_name)
                    if not hMap or hMap == INVALID_HANDLE:
                        return CPUTempInfo(source="N/A")
                    try:
                        pData = ctypes.windll.kernel32.MapViewOfFile(hMap, 0x0004, 0, 0, 0)
                        if not pData:
                            return CPUTempInfo(source="N/A")
                        try:
                            # Header: dwSignature(4), dwVersion(4), dwRevision(4),
                            #         poll_time(8), dwNumReadingElements(4), dwReadingSize(4)
                            hdr = ctypes.string_at(pData, 32)
                            sig, ver, rev = struct.unpack_from('<III', hdr, 0)
                            if sig != 0x53484649:  # 'IHFS'
                                return CPUTempInfo(source="N/A")
                            num_readings, reading_size = struct.unpack_from('<II', hdr, 24)
                            # Reading struct: dwSensorType(4), dwSensorIndex(4),
                            #   tReading(4-float), szLabelOrig(128), szLabelUser(128), szUnit(16), ...
                            READING_OFFSET = 64  # sizeof header
                            pkg = -1.0
                            cores = []
                            for i in range(num_readings):
                                off = READING_OFFSET + i * reading_size
                                raw = ctypes.string_at(pData + off, reading_size)
                                sensor_type = struct.unpack_from('<I', raw, 0)[0]
                                if sensor_type != 1:  # 1 = Temperature
                                    continue
                                val = struct.unpack_from('<f', raw, 8)[0]
                                label = raw[12:12+HWINFO_SENSORS_STRING_LEN2].split(b'\x00')[0].decode('utf-8','ignore').lower()
                                if 'cpu' not in label and 'ccd' not in label and 'tdie' not in label and 'tctl' not in label:
                                    continue
                                if 'package' in label or 'tdie' in label or 'tctl' in label:
                                    pkg = val
                                else:
                                    cores.append(val)
                            return CPUTempInfo(source="HWiNFO64", package_temp=pkg, core_temps=cores)
                        finally:
                            ctypes.windll.kernel32.UnmapViewOfFile(pData)
                    finally:
                        ctypes.windll.kernel32.CloseHandle(hMap)
                except Exception:
                    return CPUTempInfo(source="N/A")
            return _get
        except Exception:
            return None

    def get(self) -> CPUTempInfo:
        import time
        # 若目前來源是 N/A，定期重試偵測
        if self._source == "N/A" and self._retry_at and time.time() >= self._retry_at:
            new_source, new_fn = self._find_provider()
            if new_source != "N/A":
                self._source = new_source
                self._get_fn = new_fn
                self._retry_at = 0
                logger.info(f"[CPU Temp] 重新偵測成功，切換到: {new_source}")
            else:
                self._retry_at = time.time() + 30  # 繼續每 30 秒重試
        return self._get_fn()


# ─────────────────────────────────────────────────────────────────────────────
# HardwareManager — 統一入口
# ─────────────────────────────────────────────────────────────────────────────

class HardwareManager:
    """
    自動偵測硬體，統一提供 GPU / CPU 資料。
    使用 .detect() 初始化，之後呼叫 .get_gpu_list() / .get_cpu_temp()。
    """

    def __init__(self):
        self._gpu_providers: List = []
        self._cpu_temp = _CPUTempProvider()
        self._gpu_vendors: List[str] = []
        self._detect_gpu()

    # ── GPU 偵測 ──────────────────────────────────────────────────────────────

    def _detect_gpu(self):
        # 1. NVIDIA via pynvml
        try:
            import pynvml
            p = _NvidiaProvider()
            if p._count > 0:
                self._gpu_providers.append(p)
                self._gpu_vendors.append("NVIDIA/pynvml")
                logger.info("[HW] 已啟用 NVIDIA pynvml 提供者")
        except Exception as e:
            logger.debug(f"[HW] pynvml 不可用: {e}")
            # 1b. GPUtil fallback for NVIDIA
            try:
                import GPUtil
                gpus = GPUtil.getGPUs()
                if gpus:
                    p = _GPUtilProvider()
                    self._gpu_providers.append(p)
                    self._gpu_vendors.append("NVIDIA/GPUtil")
                    logger.info("[HW] 已啟用 NVIDIA GPUtil fallback 提供者")
            except Exception as e2:
                logger.debug(f"[HW] GPUtil 不可用: {e2}")

        # 2. AMD via ADL
        try:
            p = _AMDProvider()
            if p._adapters:
                self._gpu_providers.append(p)
                self._gpu_vendors.append("AMD/ADL")
                logger.info("[HW] 已啟用 AMD ADL 提供者")
        except Exception as e:
            logger.debug(f"[HW] AMD ADL 不可用: {e}")

        # 3. WMI fallback — 補上尚未偵測到的 Intel / 其他
        detected_names = set()
        for p in self._gpu_providers:
            for g in p.get_gpu_list():
                detected_names.add(g.name.lower())

        try:
            wmi_p = _WMIGPUProvider()
            wmi_gpus = wmi_p.get_gpu_list()
            extra = [g for g in wmi_gpus if g.name.lower() not in detected_names]
            if extra:
                self._gpu_providers.append(wmi_p)
                self._gpu_vendors.append("WMI")
                logger.info(f"[HW] WMI 額外偵測到 {len(extra)} 個顯示卡")
        except Exception as e:
            logger.debug(f"[HW] WMI GPU 不可用: {e}")

        if not self._gpu_providers:
            logger.warning("[HW] 未偵測到任何 GPU 提供者")

    # ── 公開 API ──────────────────────────────────────────────────────────────

    def get_gpu_list(self) -> List[dict]:
        """回傳所有 GPU 的 dict 列表（可直接 JSON 序列化）"""
        seen_names = set()
        result = []
        gid = 0
        for provider in self._gpu_providers:
            try:
                for g in provider.get_gpu_list():
                    key = g.name.lower()
                    if key in seen_names:
                        continue
                    seen_names.add(key)
                    result.append({
                        "id":          gid,
                        "name":        g.name,
                        "vendor":      g.vendor,
                        "api":         g.api,
                        "load":        g.load,
                        "mem_total":   g.mem_total,
                        "mem_used":    g.mem_used,
                        "mem_free":    g.mem_free,
                        "mem_percent": g.mem_percent,
                        "temperature": g.temperature,
                        "fan_speed":   g.fan_speed,
                        "power_usage": g.power_usage,
                        "driver":      g.driver,
                        "uuid":        g.uuid,
                        "core_clock":  g.core_clock,
                        "mem_clock":   g.mem_clock,
                    })
                    gid += 1
            except Exception as e:
                logger.warning(f"[HW] 取得 GPU 資料失敗: {e}")
        return result

    def get_cpu_temp(self) -> dict:
        """回傳 CPU 溫度資訊"""
        info = self._cpu_temp.get()
        return {
            "source":       info.source,
            "package_temp": info.package_temp,
            "core_temps":   info.core_temps,
        }

    @property
    def gpu_vendors(self) -> List[str]:
        return list(self._gpu_vendors)

    @property
    def gpu_available(self) -> bool:
        return len(self._gpu_providers) > 0

    def summary(self) -> str:
        if not self._gpu_vendors:
            return "GPU: 無"
        return "GPU: " + ", ".join(self._gpu_vendors)


# 模組層級單例（import 即初始化）
_manager: Optional[HardwareManager] = None

def get_manager() -> HardwareManager:
    global _manager
    if _manager is None:
        _manager = HardwareManager()
    return _manager
