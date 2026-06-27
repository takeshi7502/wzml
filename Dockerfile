FROM mysterysd/wzmlx:v3

WORKDIR /usr/src/app

# TEMP LOCAL TEST ONLY: base image can have dpkg metadata but miss runtime binaries.
# Revert before final source unless user explicitly wants to keep this runtime fix.
RUN apt-get update \
    && apt-get install -y --reinstall --no-install-recommends \
        aria2 \
        qbittorrent-nox \
        ffmpeg \
        rclone \
        sabnzbdplus \
        p7zip-full \
        coreutils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN uv pip install --python /wzvenv/bin/python --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["bash", "start.sh"]
