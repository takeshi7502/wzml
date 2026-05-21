from asyncio import TimeoutError, wait_for

from bot import DOWNLOAD_DIR, bot_loop
from bot.core.config_manager import Config
from bot.helper.ext_utils.bot_utils import arg_parser
from bot.helper.ext_utils.task_manager import pre_task_check
from bot.helper.ext_utils.links_utils import is_url
from bot.helper.listeners.task_listener import TaskListener
from bot.helper.mirror_leech_utils.download_utils.aria2_download import add_aria2_download
from bot.helper.mirror_leech_utils.pikpak_utils.pikpak_client import PikPakClient, is_pikpak_share_url
from bot.helper.telegram_helper.message_utils import (
    delete_links,
    edit_message,
    send_message,
    set_message_reaction,
)


class PikPakMirror(TaskListener):
    def __init__(self, client, message):
        self.client = client
        self.message = message
        self.same_dir = {}
        self.bulk = []
        self.multi_tag = None
        self.options = ""
        super().__init__()

    async def new_event(self):
        check_msg, check_button = await pre_task_check(self.message)
        if check_msg:
            await delete_links(self.message)
            await send_message(self.message, check_msg, check_button)
            return

        args = {
            "-doc": False,
            "-med": False,
            "-d": False,
            "-j": False,
            "-s": False,
            "-b": False,
            "-e": False,
            "-z": False,
            "-i": 0,
            "-sp": 0,
            "link": "",
            "-n": "",
            "-m": "",
            "-up": "",
            "-rcf": "",
            "-au": "",
            "-ap": "",
            "-h": "",
            "-t": "",
            "-ff": set(),
        }
        input_list = self.message.text.split("\n", 1)[0].split(" ")
        arg_parser(input_list[1:], args)

        self.link = args["link"].strip()
        self.name = args["-n"]
        self.up_dest = args["-up"]
        self.rc_flags = args["-rcf"]
        self.select = args["-s"]
        self.seed = args["-d"]
        self.compress = args["-z"]
        self.extract = args["-e"]
        self.join = args["-j"]
        self.thumb = args["-t"]
        self.split_size = args["-sp"]
        self.multi = int(args.get("-i", 0) or 0)
        self.is_leech = False

        if not self.link:
            await set_message_reaction(self.message, "❌")
            await send_message(self.message, "Send a PikPak path for now. Example: /pk /My Pack/video.mp4")
            await delete_links(self.message)
            return

        try:
            pikpak = PikPakClient()
            original_link = self.link
            if is_pikpak_share_url(self.link):
                status_msg = await send_message(
                    self.message,
                    "PikPak: restoring shared file to /WZML...",
                )
                download = await wait_for(pikpak.save_share_and_get_download(self.link), timeout=180)
                self.source_url = original_link
                await status_msg.delete()
            elif is_url(self.link) or self.link.startswith("magnet:"):
                status_msg = await send_message(
                    self.message,
                    "PikPak: creating save task...\nStep: checking/creating /WZML and submitting URL",
                )
                save_data = await wait_for(pikpak.save_link(self.link), timeout=45)
                task_id = save_data.get("id") or save_data.get("task_id") or save_data.get("task", {}).get("id")
                await edit_message(
                    status_msg,
                    f"PikPak: save task created{f' ({task_id})' if task_id else ''}, waiting for file...",
                )
                file_id = await pikpak.wait_saved_file(save_data, timeout=90)
                await edit_message(status_msg, f"PikPak: file saved ({file_id}), getting direct link...")
                download = await pikpak.get_download_url(file_id)
                self.source_url = original_link
                await status_msg.delete()
            else:
                info = await pikpak.resolve_path_info(self.link)
                if info.get("kind") == "drive#folder":
                    raise ValueError("PikPak folder download is not supported yet. Use a file path first.")
                download = await pikpak.get_download_url(info.get("id", ""))
                self.source_url = f"https://t.me/share/url?url={self.link}"
            if not download.get("url"):
                raise ValueError("PikPak did not return a download URL for this file.")
            self.link = download["url"]
            if not self.name:
                self.name = download.get("name", "")
            await self.before_start()
            self._set_mode_engine()
        except TimeoutError:
            await set_message_reaction(self.message, "❌")
            await send_message(
                self.message,
                "PikPak error: request timed out. Share restore can take longer if PikPak is slow; if this repeats, send me the exact error/log.",
            )
            await delete_links(self.message)
            return
        except Exception as e:
            await set_message_reaction(self.message, "❌")
            await send_message(self.message, f"PikPak error: {e}")
            await delete_links(self.message)
            return

        await set_message_reaction(self.message, "✅")
        await delete_links(self.message)
        path = f"{DOWNLOAD_DIR}{self.mid}"
        await add_aria2_download(self, path, "", "", "")


async def pikpak(client, message):
    bot_loop.create_task(PikPakMirror(client, message).new_event())
