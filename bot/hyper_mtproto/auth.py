from pyrogram.session import Auth


async def get_auth_key(client, dc_id, test_mode=None):
    if test_mode is None:
        test_mode = await client.storage.test_mode()
    main_dc = await client.storage.dc_id()
    if dc_id == main_dc:
        ak = await client.storage.auth_key()
        return ak, False
    ak = await Auth(client, dc_id, test_mode).create()
    return ak, True
