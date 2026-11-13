"""One-step launcher for this computer only: first run creates the admin, then opens the browser."""
import getpass
import os
from pathlib import Path
import socket
import sys
import webbrowser

from app import create_server
from template_manager.auth import Auth

PORT = 8765
URL = f'http://127.0.0.1:{PORT}'


def show(url):
    if os.environ.get('NO_BROWSER') != '1':
        webbrowser.open(url)


def running():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex(('127.0.0.1', PORT)) == 0


def main():
    print('无敌亚马逊大王 · 本机启动')
    if running():
        print(f'\n软件已经在运行，直接打开：{URL}')
        show(URL)
        return
    data = Path(__file__).resolve().parent / '.data' / 'library'
    auth = Auth(data.parent / 'accounts.sqlite3')
    with auth.db() as db:
        empty = db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0
    if empty:
        print('\n首次使用：创建管理员账号。')
        username = input('管理员用户名（直接回车使用 admin）：').strip() or 'admin'
        while True:
            password = getpass.getpass('管理员密码（至少 12 个字符，输入时不显示）：')
            if password != getpass.getpass('再输入一次：'):
                print('两次输入不一样，请重试。')
                continue
            try:
                auth.create(username, password, 'admin')
                print('管理员已创建：' + username)
                break
            except ValueError as e:
                print(e)
    server = create_server(data, PORT)
    print(f'\n启动成功，浏览器打开：{URL}')
    print('只有这台电脑能访问；要给同事用，请改用“启动-局域网共享.bat”。')
    print('使用期间不要关闭这个窗口；关闭窗口即停止软件。')
    show(URL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as e:
        print('\n启动失败：' + str(e))
        sys.exit(1)
