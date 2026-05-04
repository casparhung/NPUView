# NPUView - System Monitor

NPUView 是一個在 Windows 上執行的本機硬體監控儀表板。
使用 Flask + Socket.IO 提供網頁介面，預設每 2 秒更新一次 CPU、GPU、記憶體、磁碟與網路資訊。

![NPUView 系統監控儀表板截圖](image.png)

## 功能總覽

| 類別 | 內容 |
|------|------|
| CPU | 使用率、每核心使用率、時脈、CPU 溫度來源 |
| GPU | 使用率、顯存、溫度、風扇、功耗、核心/記憶體時脈 |
| 記憶體 | 實體記憶體與 Swap 使用量 |
| 磁碟 | 各分割區容量與使用率 |
| 網路 | 傳輸量、封包統計 |
| 系統 | OS、主機名稱、CPU 型號、開機時間、運行時間 |

## 支援硬體與資料來源

| 裝置 | 優先來源 | 備援來源 | 說明 |
|------|----------|----------|------|
| NVIDIA GPU | pynvml | GPUtil / WMI | 部分指標可能因驅動限制不支援 |
| AMD GPU | ADL (atiadlxx.dll) | WMI | 支援 RX 系列常見監控資訊 |
| Intel GPU | WMI | - | 以基本資訊為主 |
| CPU 溫度 | LibreHardwareMonitor / HWiNFO / OpenHardwareMonitor | psutil fallback | 若無可用來源會顯示 N/A |

## 系統需求

- Windows 10/11 (64-bit)
- Python 3.9+
- 可選工具 (用於 CPU 溫度):
    - LibreHardwareMonitor
    - OpenHardwareMonitor
    - HWiNFO64 (需開啟 Shared Memory Support)

## 快速啟動

### 方式 1: 批次檔

```bat
start_npuview.bat
```

### 方式 2: PowerShell

```powershell
.\start_npuview.ps1
```

### 方式 3: 手動

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

啟動後開啟:

```text
http://localhost:2700
```

## 啟動腳本會做什麼

兩個啟動腳本都會自動執行以下步驟:

1. 切換到專案目錄。
2. 若存在 `.venv\Scripts\python.exe`，優先使用該 Python。
3. 檢查套件是否可匯入，若缺少則執行 `pip install -r requirements.txt`。
4. 啟動 `app.py`。

## CPU 溫度說明

Windows 一般程式無法直接讀取 CPU 溫度感測器，NPUView 會嘗試以下來源:

| 工具 | 偵測方式 | 備註 |
|------|----------|------|
| LibreHardwareMonitor | WMI `root\LibreHardwareMonitor` | 建議系統管理員執行 |
| OpenHardwareMonitor | WMI `root\OpenHardwareMonitor` | 較舊但可用 |
| HWiNFO64 | Shared Memory `HWiNFO_SENS_SM2` | 需先開啟 Shared Memory |

若啟動時尚未偵測到來源，NPUView 會每 30 秒自動重試，不需要重啟程式。

## 疑難排解

### 1) 顯示 NVML Not Supported

訊息範例:

```text
pynvml.nvml.NVMLError_NotSupported: Not Supported
```

這代表某個 NVIDIA 指標在目前驅動或裝置上不支援，不是整張卡失效。
目前版本已改為自動略過不支援欄位，會繼續啟動並提供其他可用指標。

### 2) AMD/ADL 顯示 0 個活躍顯示卡

若同時可看到 NVIDIA 或 WMI 資料，通常屬正常情況，代表 ADL 沒有偵測到可用 AMD 裝置。

### 3) CPU 溫度一直是 N/A

請先啟動 LibreHardwareMonitor/OpenHardwareMonitor/HWiNFO64，並確認權限與設定正確。

## 專案結構

```text
NPUView/
|- app.py
|- hardware_provider.py
|- requirements.txt
|- start_npuview.bat
|- start_npuview.ps1
|- templates/
|  |- index.html
|- image.png
```

## 套件清單

- flask
- flask-socketio
- psutil
- GPUtil
- eventlet
- pynvml
- pywin32
- wmi
