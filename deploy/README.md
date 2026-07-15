# Деплой на VPS

Проверено против конкретного сервера (2026-07-16): Ubuntu 24.04.2, Python 3.12.3,
systemd 255, пояс **UTC**. На машине уже живут XRay (VPN, через него владелец
ходит в Telegram) и TorrServer — **их не трогаем**.

Сам VPS является выходом в Telegram, поэтому боту VPN не нужен: он пойдёт напрямую.

Команды выполняются НА СЕРВЕРЕ, кроме шага 4 — он с ноутбука.

---

## 1. Питон и venv

```bash
apt update && apt install -y python3.12-venv git
```

## 2. Код

```bash
git clone https://github.com/rkowalev/tg-bot-project.git /opt/vacancy-bot
cd /opt/vacancy-bot
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Проверка, что всё встало (сеть тесты не трогают):

```bash
.venv/bin/python -m pytest tests/ -q
```

Должно быть **234 passed**. Если нет — дальше не идти.

## 3. Секреты

```bash
nano /opt/vacancy-bot/.env
```

Содержимое — как в локальном `.env`:

```
API_ID=...
API_HASH=...
ANTHROPIC_API_KEY=...
BOT_TOKEN=...
CHAT_ID=...
```

`CHANNELS` не нужен: каналы берутся из подписок техаккаунта.

```bash
chmod 600 /opt/vacancy-bot/.env
```

## 4. Сессия Telethon и база — С НОУТБУКА

```bash
scp ~/tg-bot-project/explore.session root@5.34.215.32:/opt/vacancy-bot/
scp ~/tg-bot-project/vacancies.db     root@5.34.215.32:/opt/vacancy-bot/
```

**Зачем база, а не с нуля.** В ней лежат критерии (иначе бот попросит резюме
заново), `seen_posts` на 450 постов (иначе первый прогон переберёт их все — это
деньги и дайджест на 45 вакансий разом) и архив.

**⚠️ После копирования сессии НЕ ЗАПУСКАЙ обход локально.** Один `.session`,
работающий с двух машин одновременно, даёт `AUTH_KEY_DUPLICATED`: Telegram
отзывает авторизацию техаккаунта, чинится только логином с кодом. Локально
теперь можно всё, кроме `daily_fetch.py`, кнопки «Проверить сейчас» и
`reassess.py` на старых записях — им нужна сеть.

Отладке сеть не нужна: исходники постов лежат в БД (`raw_text`). Скачал базу с
сервера — разбирайся офлайн.

## 5. Юниты

```bash
cp /opt/vacancy-bot/deploy/vacancy-bot.service   /etc/systemd/system/
cp /opt/vacancy-bot/deploy/vacancy-fetch.service /etc/systemd/system/
cp /opt/vacancy-bot/deploy/vacancy-fetch.timer   /etc/systemd/system/
systemctl daemon-reload
```

## 6. Проверить пояс ДО запуска

```bash
systemd-analyze calendar 'Mon-Fri 07:00 Europe/Moscow'
```

В строке `Next elapse` должно быть **04:00 UTC** (= 07:00 МСК). Если UTC-время
другое — пояс не подхватился, разбираться здесь, а не после первого пропуска.

## 7. Запуск

```bash
systemctl enable --now vacancy-bot.service
systemctl enable --now vacancy-fetch.timer
```

Обход по таймеру НЕ включаем руками — он сам сработает в 7:00 МСК.

## 8. Убедиться, что живо

```bash
systemctl status vacancy-bot --no-pager        # active (running)
systemctl list-timers vacancy-fetch --no-pager # NEXT = ближайшие будни, 04:00 UTC
journalctl -u vacancy-bot -n 20 --no-pager     # "Run polling for bot @..."
```

И главное — нажать в боте любую кнопку показа. Отвечает = дошло.

Разовый обход руками, если не хочется ждать утра:

```bash
systemctl start vacancy-fetch.service
journalctl -u vacancy-fetch -f
```

## 9. Выключить локального бота

Иначе два процесса будут разбирать одни и те же нажатия: Telegram отдаёт
апдейт кому-то одному, и кнопки начнут срабатывать через раз.

```bash
pkill -f scripts/run_bot.py
```

---

## Что где смотреть потом

| зачем | команда |
|---|---|
| бот жив? | `systemctl status vacancy-bot` |
| логи бота | `journalctl -u vacancy-bot -f` |
| когда следующий обход | `systemctl list-timers vacancy-fetch` |
| как прошёл обход | `journalctl -u vacancy-fetch --since today` |
| отчёт обхода | `tail -30 /opt/vacancy-bot/logs/daily_fetch.log` |
| обновить код | `cd /opt/vacancy-bot && git pull && systemctl restart vacancy-bot` |
| забрать базу на ноут | `scp root@5.34.215.32:/opt/vacancy-bot/vacancies.db ./` |

## Грабли этого сервера

- **Пояс UTC.** Любое время в юнитах пишем с явным `Europe/Moscow`.
- **XRay и TorrServer рядом.** У юнитов стоят `MemoryMax=512M` и `CPUQuota=50%`:
  ляжет VPN — ляжет и доступ к Telegram, и бот вместе с ним.
- **Python на сервере 3.12, локально 3.14.** Проверено: 234 теста проходят на
  обеих. Не тащить в код 3.13+/3.14-фичи.
- **Вход root по паролю.** Не блокер, но 66 необновлённых пакетов и открытый
  SSH — стоит однажды закрыть ключами и `unattended-upgrades`.
