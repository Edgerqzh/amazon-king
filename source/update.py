"""Pull the latest version from GitHub. Business data (.data) is ignored by git and never touched."""
import os
from pathlib import Path
import socket
import subprocess
import sys

HERE = Path(__file__).resolve().parent
# Local proxy ports of common tools (v2rayN http, Clash Verge, Clash); tried only when listening.
PROXY_PORTS = (10809, 7897, 7890)


def git(*args, proxy=None):
    command = ['git'] + (['-c', 'http.proxy=' + proxy] if proxy else []) + list(args)
    return subprocess.run(command, cwd=HERE, capture_output=True, text=True, encoding='utf-8', errors='replace')


def listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.5):
            return True
    except OSError:
        return False


def proxies():
    """Proxies to try after a direct/configured pull fails, most specific first, without duplicates."""
    found = []
    for name in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
        value = os.environ.get(name, '').strip()
        if value:found.append(value if '://' in value else 'http://' + value)
    found += ['http://127.0.0.1:%d' % port for port in PROXY_PORTS if listening(port)]
    return list(dict.fromkeys(found))


def network_error(text):
    words = ('unable to access', 'could not resolve', 'failed to connect', 'empty reply', 'timed out', 'connection was reset', 'ssl', 'proxy')
    return any(word in text.lower() for word in words)


def main():
    print('无敌亚马逊大王 · 更新到最新版\n')
    try:
        check = git('rev-parse', '--show-toplevel')
    except FileNotFoundError:
        print('没有找到 git。请先安装 Git for Windows：https://git-scm.com/download/win')
        return 1
    if check.returncode != 0:
        print('这个文件夹不是用 git clone 下载的，不能自动更新。')
        print('请重新用 git clone 下载一份，再把旧文件夹里的 .data 文件夹复制过去。')
        return 1
    changed = [line for line in git('status', '--porcelain', '--untracked-files=no').stdout.splitlines() if line.strip()]
    if changed:
        print('下面这些程序文件被改动过，为了不覆盖你的修改，这次没有更新：')
        for line in changed[:20]:print('   ', line)
        print('\n如果这些改动不需要保留，把这几行发给开发者处理。')
        return 1
    before = git('rev-parse', 'HEAD').stdout.strip()
    print('正在连接 GitHub…')
    result = git('pull', '--ff-only')
    if result.returncode != 0 and network_error(result.stderr):
        for proxy in proxies():
            print('直连不通，改用代理 %s 再试…' % proxy)
            result = git('pull', '--ff-only', proxy=proxy)
            if result.returncode == 0 or not network_error(result.stderr):break
    if result.returncode != 0:
        print('\n更新失败：')
        print(result.stderr.strip()[-800:])
        if network_error(result.stderr):
            print('\n连不上 GitHub。请先打开代理软件（v2rayN / Clash），再双击“更新.bat”。')
        return 1
    after = git('rev-parse', 'HEAD').stdout.strip()
    if after == before:
        print('\n已经是最新版，不用更新。')
        return 0
    print('\n更新完成，这次新增：')
    print(git('log', '--format=  · %s', before + '..' + after).stdout.rstrip())
    print('\n接下来：关掉软件的黑色窗口，重新双击“启动.bat”，然后在浏览器里按 Ctrl+F5 刷新。')
    print('账号、模板库等数据都在 .data 文件夹里，更新不会动它们。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
