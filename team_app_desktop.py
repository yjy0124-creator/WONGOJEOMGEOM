"""팀 원고 점검 프로그램을 브라우저 없이 독립 실행형 창으로 띄운다.

내부적으로는 `team_app.py`의 기존 HTTP 서버(TeamApplication)를 백그라운드
스레드에서 그대로 구동하고, pywebview로 만든 네이티브 창이 그 로컬 주소를
띄운다. HTML/CSS 화면은 재사용하되 사용자는 브라우저 주소창을 볼 필요가
없다. PyInstaller로 --onefile 실행 파일로 묶어 팀원 각자 PC에 배포한다.
"""

from __future__ import annotations

import ctypes
import socket
import sys
import threading
import webbrowser
import winreg
from contextlib import closing
from pathlib import Path

import webview

from team_app import TeamApplication, _handler
from http.server import ThreadingHTTPServer

APP_TITLE = "팀 원고 점검 프로그램"

# WebView2 Evergreen 런타임의 클라이언트 GUID (Microsoft 공식 문서 기준) —
# 이 키가 레지스트리에 있고 버전이 0.0.0.0이 아니면 런타임이 설치된 것이다.
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _webview2_installed() -> bool:
    registry_paths = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
    ]
    for hive, subkey in registry_paths:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def _warn_missing_webview2() -> bool:
    """WebView2 런타임이 없을 때 안내창을 띄운다. 계속 진행할지 여부를 반환한다."""
    message = (
        "이 프로그램의 화면을 표시하려면 Microsoft Edge WebView2 런타임이 필요합니다.\n\n"
        "설치되어 있지 않은 것으로 보입니다. 지금 다운로드 페이지를 열까요?\n"
        "(취소를 누르면 설치 없이 계속 실행을 시도합니다.)"
    )
    MB_YESNO = 0x04
    MB_ICONWARNING = 0x30
    IDYES = 6
    response = ctypes.windll.user32.MessageBoxW(0, message, APP_TITLE, MB_YESNO | MB_ICONWARNING)
    if response == IDYES:
        webbrowser.open(WEBVIEW2_DOWNLOAD_URL)
        return False
    return True


def main() -> None:
    if not _webview2_installed() and not _warn_missing_webview2():
        return

    data_root = _app_dir() / "team_data"
    application = TeamApplication(data_root)
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(application))

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    window = webview.create_window(
        APP_TITLE, f"http://127.0.0.1:{port}/", width=1440, height=900, min_size=(960, 600),
    )

    def _shutdown() -> None:
        server.shutdown()
        server.server_close()
        application.executor.shutdown(wait=False, cancel_futures=False)

    window.events.closed += _shutdown
    webview.start()


if __name__ == "__main__":
    main()
