"""Reset an account's password on this computer. The new password is typed here and never shown."""
import getpass
from pathlib import Path

from template_manager.auth import Auth


def main():
    print('无敌亚马逊大王 · 重置账号密码\n')
    path = Path(__file__).resolve().parent / '.data' / 'accounts.sqlite3'
    if not path.is_file():
        print('还没有任何账号。直接双击“启动.bat”，第一次启动时会让你创建管理员。')
        return
    auth = Auth(path)
    with auth.db() as db:
        users = db.execute('SELECT username, role FROM users ORDER BY username').fetchall()
    if not users:
        print('还没有任何账号。直接双击“启动.bat”，第一次启动时会让你创建管理员。')
        return
    roles = {'admin': '管理员', 'editor': '编辑者', 'viewer': '只读'}
    for index, (name, role) in enumerate(users, 1):
        print('  %d. %s（%s）' % (index, name, roles.get(role, role)))
    choice = input('\n要重置哪个账号？输入序号或用户名（只有一个账号时直接回车）：').strip()
    if not choice and len(users) == 1:
        username = users[0][0]
    elif choice.isdigit() and 1 <= int(choice) <= len(users):
        username = users[int(choice) - 1][0]
    else:
        username = choice
    while True:
        password = getpass.getpass('新密码（至少 12 个字符，输入时不显示）：')
        if password != getpass.getpass('再输入一次：'):
            print('两次输入不一样，请重试。')
            continue
        try:
            auth.set_password(username, password)
        except ValueError as e:
            print(e)
            if '没有这个账号' in str(e):
                return
            continue
        break
    print('\n已重置“%s”的密码，这个账号在所有浏览器里都已退出，请用新密码登录。' % username)


if __name__ == '__main__':
    main()
