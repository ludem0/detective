#!/bin/bash
# Запускать на сервере из папки проекта: bash deploy.sh
set -e

if [ ! -f .env ]; then
  echo "Нет .env! Скопируй .env.example -> .env и задай ADMIN_PASSWORD." >&2
  exit 1
fi

# База игры лежит в ./data; контейнер пишет под пользователем uid 10001.
mkdir -p data
chown 10001:10001 data

echo "==> Собираю и запускаю..."
docker compose up -d --build

echo "==> Статус:"
docker compose ps
echo ""
echo "Готово: http://$(hostname -I | awk '{print $1}'):8083"
