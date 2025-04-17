#!/bin/bash

echo "⚙️ Đang build lại Docker image mrbot..."
if ! sudo docker build . -t mrbot; then
  echo "❌ Build thất bại. Dừng quá trình."
  exit 1
fi

echo "🛑 Dừng container cũ (nếu có)..."
sudo docker stop mrbot_container 2>/dev/null

echo "🧹 Xoá container cũ..."
sudo docker rm mrbot_container 2>/dev/null

# Phương pháp 1: Sử dụng --restart=always để tự động khởi động lại container
echo "🚀 Khởi chạy container mới với --restart=always..."
sudo docker run -d --name mrbot_container --restart=always mrbot

echo "✅ Hoàn tất! BOT đã sẵn sàng hoạt động."
echo "🔗 Để xem log, bạn có thể sử dụng lệnh sau: sudo docker logs -f mrbot_container"


# Phương pháp 2: Sử dụng systemd để tự động khởi động lại container (ổn hơn)
# 1. Tạo file dịch vụ systemd : sudo nano /etc/systemd/system/mrbot.service
# 2. Thêm nội dung sau vào file dịch vụ: Ctrl + / (để bỏ #)
######################################################
# [Unit]
# Description=mrbot Docker Container
# Requires=docker.service
# After=docker.service

# [Service]
# Restart=always
# ExecStart=/usr/bin/docker run --name mrbot_container mrbot
# ExecStop=/usr/bin/docker stop mrbot_container
# ExecStopPost=/usr/bin/docker rm mrbot_container

# [Install]
# WantedBy=default.target
######################################################
# 3. Lưu file và thoát (Ctrl + X, Y, Enter)
# 4. Tải lại systemd để nhận diện file dịch vụ mới: sudo systemctl daemon-reload
# 5. Khởi động dịch vụ: sudo systemctl start mrbot
# 6. Để dịch vụ tự động khởi động khi khởi động hệ thống: sudo systemctl enable mrbot

# 7. Để dừng dịch vụ: sudo systemctl stop mrbot
# 8. Để khởi động lại dịch vụ: sudo systemctl restart mrbot
# 9. Để tắt tự động khởi động dịch vụ: sudo systemctl disable mrbot
