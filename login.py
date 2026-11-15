"""Вход в Playerok. Сохраняет токен в файл .env.

Способ 1 (автоматически): вход по коду из письма.
Способ 2 (если способ 1 не работает): вставить токен из браузера вручную.
"""
import asyncio
from pathlib import Path

from PlayerokAPI import Account

ENV = Path(".env")

MANUAL_HELP = """
Как взять токен из браузера (на компьютере, Chrome или Edge):
 1. Откройте https://playerok.com и войдите в свой аккаунт.
 2. Нажмите F12, вверху откройте вкладку "Application" (или "Приложение").
    Если вкладки не видно, нажмите ">>" справа от вкладок.
 3. Слева: Storage -> Cookies -> https://playerok.com
 4. В списке найдите строку с именем  token  и скопируйте её Value
    (длинная строка из букв и цифр, двойной клик по значению, Ctrl+C).
 5. Вставьте сюда (Ctrl+V или правая кнопка мыши) и нажмите Enter.
Токен никому не показывайте и не отправляйте: он даёт доступ к аккаунту.
"""


def save_token(token: str) -> None:
    lines = ENV.read_text("utf-8").splitlines() if ENV.exists() else []
    lines = [l for l in lines if not l.startswith("PLAYEROK_TOKEN=")]
    lines.append(f"PLAYEROK_TOKEN={token}")
    ENV.write_text("\n".join(lines) + "\n", "utf-8")


async def login_by_code() -> str:
    email = input("Почта, которую вы используете для входа на Playerok: ").strip()
    async with Account() as acc:
        await acc.auth.send_otp(email)
        code = input("Код из письма: ").strip()
        await acc.auth.confirm_otp(email, code)
        if not acc.token:
            code2 = input("Включена двухфакторная защита. Введите код 2FA: ").strip()
            await acc.auth.confirm_second_factor(code2)
        return acc.token or ""


async def check_token(token: str) -> bool:
    try:
        async with Account(token=token) as acc:
            me = await acc.get_me()
            print(f"Токен рабочий. Аккаунт: {me.username}")
            return True
    except Exception as e:
        print(f"Токен не подошёл: {e}")
        return False


async def main() -> None:
    print("Пробую войти по коду из письма...")
    token = ""
    try:
        token = await login_by_code()
    except Exception as e:
        print(f"\nВход по коду не сработал ({type(e).__name__}).")
        print("Ничего страшного, используем токен из браузера.")

    if not token:
        print(MANUAL_HELP)
        token = input("Вставьте токен: ").strip().strip('"').strip("'")

    if not token:
        raise SystemExit("Токен не введён.")
    if not await check_token(token):
        raise SystemExit("Проверьте, что скопировали значение cookie 'token' целиком, и запустите снова.")
    save_token(token)
    print("Готово! Токен сохранён в .env. Теперь запускайте: python bot.py")


asyncio.run(main())
