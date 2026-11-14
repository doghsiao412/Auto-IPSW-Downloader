# Auto IPSW Downloader

自動下載 Apple 最新、仍在簽署中的 IPSW 韌體（iPhone、iPad、Mac），驗證後放到你指定的地方，並在有新版本和下載完成時通知你。適合需要隨時備有最新韌體來回復裝置的維修或 IT 工作。

韌體資料由 [IPSW.me](https://ipsw.me) 提供，檔案直接從 Apple 下載。

## 兩種用法

| | Mac 版 | Docker 版（Synology NAS） |
|---|---|---|
| 適合 | 直接在維修用的 Mac 上備好韌體 | NAS 全天候下載，存到外接硬碟帶著走 |
| 韌體放在 | Finder／iTunes 回復時讀取的資料夾 | 外接 USB 硬碟的 `IPSW-Auto/` |
| 介面 | 原生 App 視窗 | 網頁 |
| 通知 | Mac 系統通知＋郵件 | DSM 郵件 |
| 安裝 | 下載 dmg，拖進「應用程式」 | 執行部署腳本 |
| 說明 | 本頁下方 | [docker/README.md](docker/README.md) |

兩個版本共用同一套下載核心（`server.py`、`notifications.py`、`web/`），沒有任何第三方 Python 套件。

## 共同功能

- 只下載各系統最高正式版、且 Apple 仍在簽署的 IPSW；不收 beta、OTA 或較舊分支。
- 可選裝置類別，也可指定機型；機型留白代表該類別的全部機型。
- 最多同時下載 5 個檔案，名額一空出來就遞補；支援中斷續傳。
- 每個檔案以大小、SHA-1 和可用的 SHA-256 驗證，通過才算完成。
- 下載速度低於期望值持續 2 分鐘，只對該檔案重新連線續傳。
- 有新版時自動清除舊版；可選擇先刪舊版再下載以節省空間。只會刪除本工具自己放的檔案。
- 偵測到新版本、以及全部下載完成時通知。

IPSW.me 的資料可能比 Apple 發布稍晚，無法保證發布當下立即偵測。

## Mac 版

從 [Releases](../../releases) 下載 `Auto-IPSW-Downloader-Mac-*.dmg`。需要 Apple 晶片的 Mac、macOS 13 以上。

同一套程式也能直接在 Mac 上執行（不需要 Docker），韌體會放進 Finder／iTunes 回復時讀取的資料夾：

| 裝置 | 資料夾（位於 `~/Library/iTunes/`） |
|---|---|
| iPhone | `iPhone Software Updates` |
| iPad | `iPad Software Updates` |
| Mac | `Mac Software Updates` |

- 安裝：把 `Auto IPSW Downloader.app` 拖進「應用程式」並打開。它會自動安裝背景服務，開機登入後自動啟動；視窗裡可以開關自動下載、選裝置與機型、看進度。關掉視窗不會停止下載。
- 第一次開啟：App 沒有 Apple 公證，需到「系統設定 › 隱私權與安全性」按「強制打開」。若 Mac 還沒有「命令列開發者工具」，App 會引導安裝。
- 更新：用新版 App 取代舊的再打開一次即可。
- 郵件通知：在 App 的「通知」按「設定寄信」。密碼只存進這台 Mac 的鑰匙圈。沒設定郵件時只有系統通知。
- 打包 App：`sh mac/build-app.sh`（需要 Xcode），成品在 `dist/`，只支援 Apple 晶片的 Mac（macOS 13 以上）。不透過 App 也可以用 `sh mac/install.sh [位址]` 安裝，進階設定網頁在 `http://127.0.0.1:18766`。
- 資料夾裡原本就有、內容與 Apple 相同的檔案會直接沿用，不重新下載；清除舊版只會動本工具放進去的檔案。
- 下載期間會阻止 Mac 因閒置而睡眠；蓋上螢幕仍會睡眠，喚醒後自動續傳。
- 程式與資料在 `~/Library/Application Support/Auto IPSW Downloader/`，紀錄檔是其中的 `data/service.log`。
- 移除：App 的「更多 › 移除背景服務」，或執行 `sh mac/uninstall.sh`。韌體、設定與紀錄會保留。

## 專案結構

| 路徑 | 內容 |
|---|---|
| `server.py`、`notifications.py`、`web/` | 共用的下載核心與網頁介面 |
| `mac/`、`mac_backend.py` | Mac 版：原生 App、安裝腳本、存入 iTunes 資料夾的後端 |
| `docker/` | Docker 版：映像檔、Compose、NAS 部署腳本、外接硬碟複製程序 |
| `tests/` | 自動測試 |

## 測試

```
python3 -m unittest discover -s tests
```

只在臨時資料夾操作，不會下載韌體、不會動到真實硬碟。部分 USB 相關測試只能在 Linux 執行，在 Mac 上會自動略過。

## 版本

版本號寫在 `server.py` 的 `VERSION`，網頁頁尾顯示目前版本；發布時在 git 打上相同的 `vX.Y` 標籤。

- 1.5：新增 Mac 版（原生 App 視窗）。不用 Docker，直接在 Mac 背景執行，驗證後的韌體依裝置類別放進 `~/Library/iTunes` 的韌體資料夾；通知改為 Mac 系統通知加郵件。NAS 版行為不變。
- 1.4：可選「偵測到新版時，先刪除舊版再下載」。只刪除本工具管理、且所有適用機型在這次清單都有新版的舊檔；清除與下載同時進行，複製排在清除之後。預設關閉。
- 1.3：下載名額持續遞補。最多同時下載 5 個；下載完成的檔案排隊等外接硬碟（最多 10 個，另受內部暫存空間限制），複製期間不再佔用下載名額，也不會擋住新的下載。
- 1.2：設定「每個檔案的期望下載速度」（預設 2 MB/s，0 為關閉）；低於期望值持續 2 分鐘，只對該檔案重新連線並從中斷處續傳。連線中斷也會在同一檔案重試，連續 5 次沒有進度才停止本輪。
- 1.1：網頁重新掛載外接硬碟（退出後不必到現場拔插；硬碟已實體拔除時仍須重新插上）。
- 1.0：網頁安全退出外接硬碟；下載紀錄區分本次新增與原本已存在的檔案；暫存韌體損壞時自動重新下載；容器健康檢查。
