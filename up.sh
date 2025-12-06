#!/bin/bash

echo "📥 Pulling latest code..."
git pull

echo "🛑 Stopping existing containers..."
docker compose down

echo "🐳 Rebuilding docker container..."
docker compose up -d --build

echo "📄 Showing last 50 lines of logs..."
docker compose logs --tail=50

echo "🔍 Live log (Ctrl + C to exit):"
docker compose logs -f
