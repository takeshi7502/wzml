FROM mysterysd/wzmlx:v3

WORKDIR /usr/src/app

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
RUN test -x /wzvenv/bin/python || uv venv /wzvenv \
    && uv pip install --python /wzvenv/bin/python --no-cache-dir -r requirements.txt

ENV PATH="/wzvenv/bin:${PATH}"

COPY . .

ENTRYPOINT ["bash", "start.sh"]
