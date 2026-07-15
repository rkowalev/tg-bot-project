#!/usr/bin/env bash
#
# Обновление кода на сервере. Запускать НА СЕРВЕРЕ или с ноутбука одной строкой:
#   ssh root@5.34.215.32 /opt/vacancy-bot/deploy/update.sh
#
# Почему не автопулл по таймеру. Тогда любой запушенный коммит уезжает в прод
# без проверки: сломал импорт — бот молча лёг, а узнаешь ты об этом, когда
# нажмёшь кнопку и ничего не случится. Здесь ворота — тесты: не прошли,
# рестарта нет, на сервере продолжает работать старая РАБОЧАЯ версия.
#
# set -e ровно ради этого: любая неудачная команда обрывает скрипт ДО рестарта.
set -euo pipefail

cd /opt/vacancy-bot

echo "=== код ==="
git pull

echo "=== зависимости ==="
# идемпотентно: если requirements.txt не менялся, отработает мгновенно
.venv/bin/pip install -q -r requirements.txt

echo "=== тесты (ворота) ==="
.venv/bin/python -m pytest tests/ -q

echo "=== рестарт бота ==="
# Обход тут ронять не страшно: замок снимает ОС вместе с процессом, а
# недособранное подберётся на следующем прогоне — дедуп по message_id не даст
# заплатить за это дважды.
systemctl restart vacancy-bot.service
sleep 2
systemctl is-active vacancy-bot.service

echo "=== готово ==="
systemctl status vacancy-bot.service --no-pager | head -4
