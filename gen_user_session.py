#!/usr/bin/env python3
"""Generate a Pyrogram USER_SESSION_STRING manually.

Run this from the project root:
    python scripts/gen_user_session.py

For VPS/datacenter blocked logins, run it locally from a trusted/home IP, then copy
only the generated string into config.py or the database.
"""

from asyncio import run
from getpass import getpass
from importlib import import_module

from pyrogram import Client
from pyrogram.errors import PhoneCodeExpired, PhoneCodeInvalid, SessionPasswordNeeded


async def main():
    try:
        config = import_module("config")
        api_id = int(getattr(config, "TELEGRAM_API", 0) or 0)
        api_hash = getattr(config, "TELEGRAM_HASH", "") or ""
    except Exception:
        api_id = 0
        api_hash = ""

    if not api_id:
        api_id = int(input("TELEGRAM_API: ").strip())
    if not api_hash:
        api_hash = input("TELEGRAM_HASH: ").strip()

    phone_number = input("Phone number with country code (example +849xxxxxxxx): ").strip()

    client = Client(
        name="wz_manual_user_session",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True,
        app_version="@WZML_X User Session",
        device_model="@WZML_X Bot V3",
        system_version="@WZML_X Pyro Server",
    )

    await client.connect()
    try:
        sent_code = await client.send_code(phone_number)
        while True:
            phone_code = "".join(
                filter(str.isdigit, input("Telegram login code: ").strip())
            )
            try:
                await client.sign_in(
                    phone_number,
                    sent_code.phone_code_hash,
                    phone_code,
                )
                break
            except SessionPasswordNeeded:
                password = getpass("Telegram 2FA password: ")
                await client.check_password(password)
                break
            except PhoneCodeInvalid:
                print("Invalid code. Please enter the newest code again.")
            except PhoneCodeExpired:
                print("Code expired. Requesting a fresh code...")
                sent_code = await client.send_code(phone_number)

        session_string = await client.export_session_string()
        print("\nUSER_SESSION_STRING:\n")
        print(session_string)
        print("\nCopy this string into config.py or save it in the bot database.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    run(main())
