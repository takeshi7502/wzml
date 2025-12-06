import asyncio
import time
from uuid import uuid4
from urllib.parse import urlparse

import httpx

from bot import LOGGER
from bot.helper.telegram_helper.message_utils import sendMessage
from bot.helper.telegram_helper.button_build import ButtonMaker

# key -> final direct URL (mirror đã chọn)
SF_URL_CACHE = {}

# Danh sách mirror với slug (dùng cho use_mirror)
SF_MIRRORS = [
    # Auto-select (để SourceForge tự chọn)
    {"label": "🌍 Auto-select (SourceForge)", "slug": None},

    # US / North America (ưu tiên vì VPS US)
    {"label": "🇺🇸 GigeNET (IL, US)", "slug": "gigenet"},
    {"label": "🇺🇸 Psychz (NY, US)", "slug": "psychz"},
    {"label": "🇺🇸 Cytranet (TX, US)", "slug": "cytranet"},
    {"label": "🇺🇸 VersaWeb (NV, US)", "slug": "versaweb"},
    {"label": "🇺🇸 PhoenixNAP (AZ, US)", "slug": "phoenixnap"},
    {"label": "🇺🇸 Pilotfiber (NY, US)", "slug": "pilotfiber"},
    {"label": "🇺🇸 NetActuate (NC, US)", "slug": "netactuate"},
    {"label": "🇺🇸 Cfhcable (FL, US)", "slug": "cfhcable"},

    # Europe
    {"label": "🇩🇪 NetCologne (DE)", "slug": "netcologne"},
    {"label": "🇫🇷 Free.fr (FR)", "slug": "freefr"},
    {"label": "🇸🇪 AltusHost (SE)", "slug": "altushost-swe"},
    {"label": "🇧🇬 NetIX (BG)", "slug": "netix"},
    {"label": "🇧🇬 AltusHost (BG)", "slug": "altushost-sofia"},
    {"label": "🇱🇻 DEAC (LV)", "slug": "deac-riga"},
    {"label": "🇷🇸 UNLIMITED.RS (RS)", "slug": "unlimited"},

    # Asia
    {"label": "🇭🇰 Zenlayer (HK)", "slug": "zenlayer"},
    {"label": "🇸🇬 OnboardCloud (SG)", "slug": "onboardcloud"},
    {"label": "🇹🇼 TWDS (TW)", "slug": "twds"},
    {"label": "🇮🇳 Web Werks (IN)", "slug": "webwerks"},
    {"label": "🇮🇳 Excell Media (IN)", "slug": "excellmedia"},
    {"label": "🇮🇳 Cyfuture (IN)", "slug": "cyfuture"},
    {"label": "🇹🇼 NCHC (TW)", "slug": "nchc"},
    {"label": "🇯🇵 JAIST (JP)", "slug": "jaist"},
    {"label": "🇦🇿 YER (AZ)", "slug": "yer"},

    # Africa / South America / Oceania
    {"label": "🇰🇪 Liquid Telecom (KE)", "slug": "liquidtelecom"},
    {"label": "🇰🇪 Icolo (KE)", "slug": "icolo"},
    {"label": "🇦🇷 SiTSA (AR)", "slug": "sitsa"},
    {"label": "🇧🇷 SinalBR (BR)", "slug": "sinalbr"},
    {"label": "🇪🇨 Fly Life (EC)", "slug": "flylife-ec"},
    {"label": "🇦🇺 IX Australia (AU)", "slug": "ix"},
]


def _extract_project_and_relpath(url: str):
    """
    Tách projectname và rel_path từ các dạng link SourceForge thường gặp.
    Hỗ trợ:
    - https://sourceforge.net/projects/<proj>/files/<path>/file.zip/download
    - https://downloads.sourceforge.net/project/<proj>/<path>/file.zip
    """
    try:
        p = urlparse(url)
    except Exception as e:
        LOGGER.error(f"[SF] urlparse lỗi cho {url}: {e}")
        return None, None

    path = p.path or ""

    # Dạng: /projects/<proj>/files/.../download
    if path.startswith("/projects/"):
        parts = path.split("/")
        # ['', 'projects', proj, 'files', ... 'download?']
        if len(parts) < 4:
            return None, None

        project = parts[2]

        try:
            files_idx = parts.index("files")
        except ValueError:
            return None, None

        rel_parts = parts[files_idx + 1 :]
        # Bỏ "download" ở cuối nếu có
        if rel_parts and rel_parts[-1] == "download":
            rel_parts = rel_parts[:-1]

        if not rel_parts:
            return None, None

        rel_path = "/".join(rel_parts)
        return project, rel_path

    # Dạng: /project/<proj>/<path>/file.zip (downloads.sourceforge.net)
    if path.startswith("/project/"):
        parts = path.split("/")
        # ['', 'project', proj, ...]
        if len(parts) < 4:
            return None, None
        project = parts[2]
        rel_parts = parts[3:]
        rel_path = "/".join(rel_parts)
        return project, rel_path

    return None, None


async def _ping_url(client: httpx.AsyncClient, url: str):
    """
    Đo time-to-first-byte cho một URL, dùng HEAD.
    Trả về số giây (float) hoặc None nếu lỗi/timeout.
    """
    start = time.monotonic()
    try:
        await client.head(url, follow_redirects=True)
        elapsed = time.monotonic() - start
        return elapsed
    except Exception:
        return None


async def build_sf_menu(project: str, rel_path: str):
    """
    Ping tất cả mirror và build text + keyboard.
    Trả về (text, reply_markup)
    """
    base_url = f"https://downloads.sourceforge.net/project/{project}/{rel_path}"

    results = []
    async with httpx.AsyncClient(timeout=10) as client:
        tasks = []
        urls = []
        for m in SF_MIRRORS:
            slug = m["slug"]
            if slug:
                url = f"{base_url}?use_mirror={slug}"
            else:
                url = base_url
            urls.append((m, url))
            tasks.append(_ping_url(client, url))

        ping_values = await asyncio.gather(*tasks, return_exceptions=True)

    for (m, url), ping_val in zip(urls, ping_values):
        if isinstance(ping_val, Exception):
            ping_val = None
        results.append(
            {
                "label": m["label"],
                "slug": m["slug"],
                "url": url,
                "ping": ping_val,
            }
        )

    # sort: mirror có ping != None lên trước, rồi tới None, ping nhỏ trước
    results.sort(
        key=lambda x: (
            x["ping"] is None,
            x["ping"] if x["ping"] is not None else 0,
        )
    )

    btn = ButtonMaker()
    for r in results:
        ping_txt = "timeout" if r["ping"] is None else f"{r['ping']:.2f}s"
        label = f"{r['label']} ({ping_txt})"
        key = uuid4().hex[:8]
        SF_URL_CACHE[key] = r["url"]
        btn.ibutton(label, f"sfmirror|{key}")

    text = (
        f"📦 <b>File:</b> <code>{rel_path}</code>\n"
        "⚡ <b>Chọn server SourceForge để mirror (sắp xếp theo ping):</b>"
    )

    return text, btn.build_menu(2)


async def handle_sourceforge(url: str, message):
    """
    Được gọi từ mirror_leech khi phát hiện link SourceForge.
    - Gửi tin nhắn "đang lấy danh sách server..."
    - Ping mirrors, build menu
    - Edit lại chính tin nhắn đó thành list server
    """
    project, rel_path = _extract_project_and_relpath(url)
    if not project or not rel_path:
        LOGGER.warning(f"[SF] Không parse được project/rel_path từ: {url}")
        return False

    LOGGER.info(f"[SF] SourceForge detected: project={project} rel_path={rel_path}")

    # Gửi placeholder trước cho user thấy bot đã nhận lệnh
    placeholder = await sendMessage(
        message,
        "🔍 <b>Phát hiện link SourceForge</b>\n"
        "⏳ Đang kiểm tra danh sách server, đợi tí...",
    )

    try:
        text, markup = await build_sf_menu(project, rel_path)
        await placeholder.edit_text(text, reply_markup=markup)
        return True
    except Exception as e:
        LOGGER.error(f"[SF] Lỗi khi build/edit menu: {e}")
        # Báo lỗi ngay trên chính message đó, rồi cho mirror_leech xử lý link như bình thường
        try:
            await placeholder.edit_text(
                "❌ Lỗi khi lấy danh sách server SourceForge.\n"
                "➡️ Sẽ mirror trực tiếp link gốc."
            )
        except Exception:
            pass
        return False